#!/usr/bin/env python3
"""Real TreeSHAP of a local, measured PostgreSQL foreground-TPS model.

The original repository's SHAP loader refers to absent MySQL model files.
This PG-specific model is trained ONLY from audited measured case/profile
pairs, never LLM text, synthetic labels or later tuning outcomes. It predicts
pressure_control_tps / baseline_control_tps. SHAP explains that prediction,
not causality or the LLM's reasoning, and cannot promise a parameter benefit.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = ROOT / "models" / "sysinsight_pg12_tps"
SCHEMA = "keeninsight.shap.v1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_rates(path):
    """Read measured inclusive rates. Missing symbols mean zero sampled hits."""
    values = {}
    with Path(path).open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            name = row.get("Function")
            raw = row.get("Sampling Rate (%)")
            if not name or raw is None:
                continue
            value = float(str(raw).rstrip("%"))
            if not math.isfinite(value) or value < 0:
                raise ValueError("invalid sampled function rate")
            values[name] = value
    if len(values) < 10 or not any(key in values for key in ("PostgresMain", "postgres", "standard_ExecutorRun")):
        raise ValueError("not a complete PostgreSQL function-count profile")
    return values


def associations():
    data = json.loads((ROOT / 'db_profiles/postgresql/common/function_parameter_association.json').read_text())
    mapping = {}
    for item in data:
        for evidence in item.get('source_evidence', []):
            if evidence.get('function'):
                mapping.setdefault(evidence['function'], set()).add(item['knob_name'])
    return mapping


def training_rows(source):
    records, excluded, fingerprints = [], [], set()
    for path in sorted(Path(source).glob('**/case_result.json')):
        try:
            case = json.loads(path.read_text())
            decision = case.get('decision') or {}
            baseline = float(decision['baseline_control_tps'])
            pressure = float(decision['pressure_control_tps'])
            if decision.get('metric') != 'normal_control_tps' or baseline <= 0 or pressure < 0:
                raise ValueError('target is not a real foreground control TPS pair')
            if not math.isfinite(baseline + pressure):
                raise ValueError('non-finite target')
            workers = (case.get('anomaly',{}).get('all_process_metrics') or {}).get('workers',[])
            controls = [float(w['metrics']['tps']) for w in workers
                        if 'control' in w.get('app_name','') and w.get('returncode') == 0 and 'tps' in w.get('metrics',{})]
            if len(controls)!=1 or abs(controls[0]-pressure)>1e-6 or abs(float(case['baseline_metrics']['tps'])-baseline)>1e-6:
                raise ValueError('target must match the successful foreground worker and measured baseline, not external TPS')
            counts = path.parent / 'anomaly' / 'anomaly_counts_tpcc.txt'
            rates = read_rates(counts)
            signature = sha(counts)
            if signature in fingerprints:
                raise ValueError('duplicate profile, not an independent sample')
            perf = case.get('anomaly', {}).get('samples', {}).get('perf', {})
            if perf.get('status') != 'completed':
                raise ValueError('profile has no completed measured perf provenance')
            fingerprints.add(signature)
            records.append({'group': case['id'], 'target': pressure / baseline, 'rates': rates,
                'sourceCase': str(path.resolve()), 'caseSha256': sha(path),
                'profile': str(counts.resolve()), 'profileSha256': signature,
                'baselineControlTps': baseline, 'pressureControlTps': pressure,
                'foregroundWorker': next(w['app_name'] for w in workers if 'control' in w.get('app_name','') and w.get('returncode')==0),
                'captureStartedAt': perf.get('started_at'), 'captureStoppedAt': perf.get('stopped_at')})
        except (KeyError, TypeError, ValueError, OSError) as exc:
            excluded.append({'sourceCase': str(path), 'reason': str(exc)})
    if len(records) < 15 or len({r['group'] for r in records}) < 8:
        raise ValueError('Need >=15 genuine measured profiles and >=8 scenario groups; do not synthesize missing training data')
    return records, excluded


def train(source, destination):
    import joblib
    import numpy as np
    import shap
    import sklearn
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.model_selection import GroupKFold
    from sklearn.metrics import mean_absolute_error, r2_score

    records, excluded = training_rows(source)
    mapping = associations()
    # Fixed source-derived feature schema, not a target-selected feature list.
    names = sorted(mapping)
    matrix = np.array([[r['rates'].get(name, 0.0) for name in names] for r in records], dtype=float)
    target = np.array([r['target'] for r in records])
    groups = np.array([r['group'] for r in records])
    make_model = lambda: RandomForestRegressor(n_estimators=96, max_depth=5, min_samples_leaf=2,
                                               max_features=.5, n_jobs=1, random_state=20261023)
    predictions = np.zeros(len(records))
    folds = []
    for train_idx, test_idx in GroupKFold(n_splits=5).split(matrix, target, groups):
        model = make_model().fit(matrix[train_idx], target[train_idx])
        predictions[test_idx] = model.predict(matrix[test_idx])
        folds.append({'trainingGroups': sorted(set(groups[train_idx])), 'heldOutGroups': sorted(set(groups[test_idx]))})
    model = make_model().fit(matrix, target)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    archive_model_assets(destination)
    joblib.dump(model, destination / 'forest.joblib', compress=3)
    # A reference model is a versioned derived asset, not retained operational SQL history.
    dataset = {'schemaVersion': SCHEMA, 'featureNames': names,
        'records': [{**{k:v for k,v in r.items() if k!='rates'}, 'values': matrix[i].tolist(),
                     'heldOutPrediction': float(predictions[i])} for i,r in enumerate(records)], 'excluded': excluded}
    (destination / 'training_provenance.json').write_text(json.dumps(dataset,ensure_ascii=False,indent=2))
    metrics = {'method':'GroupKFold, five folds grouped by scenario ID; no same-scenario train/test overlap',
        'folds':folds, 'maeRetentionRatio':float(mean_absolute_error(target,predictions)),
        'maePercentagePoints':float(mean_absolute_error(target,predictions)*100),
        'r2':float(r2_score(target,predictions)), 'rows':len(records), 'scenarioGroups':len(set(groups))}
    version = 'pg12-foreground-tps-' + sha(destination / 'forest.joblib')[:16]
    manifest = {'schemaVersion':SCHEMA,'modelVersion':version,'modelType':'RandomForestRegressor',
        'createdAt':dt.datetime.now(dt.timezone.utc).isoformat(),
        'target':'foreground pressure TPS / foreground normal baseline TPS', 'outputUnit':'TPS retention ratio',
        'featureUnit':'original inclusive perf sample-rate units, not CPU utilization percent',
        'featureNames':names,'featureMin':matrix.min(axis=0).tolist(),'featureMax':matrix.max(axis=0).tolist(),
        'targetMin':float(target.min()),'targetMax':float(target.max()),
        'validation':metrics,'modelSha256':sha(destination/'forest.joblib'),
        'datasetSha256':sha(destination/'training_provenance.json'),
        'versions':{'sklearn':sklearn.__version__,'shap':shap.__version__,'numpy':np.__version__,'joblib':joblib.__version__},
        'scope':'Experimental PG12 local TPC-C foreground business under external pressure; combined captured backend profile.',
        'limitations':['Limited local historical benchmark data; held-out validation is not an online guarantee.',
                       'Tree-path-dependent TreeSHAP explains this fitted model using training path counts, not causation or the LLM.',
                       'Not calibrated for TPC-DS/AP-only workloads or different PostgreSQL builds.',
                       'Missing symbols in a complete counts file are zero sampled hits, not zero execution cost.']}
    (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    archive_model_assets(destination)
    return {'modelVersion':version,'validation':metrics,'featureCount':len(names),'destination':str(destination)}


def archive_model_assets(directory):
    """Retain immutable model/data pairs so historical SHAP hashes stay auditable."""
    directory=Path(directory)
    if not (directory/'manifest.json').is_file():
        return
    manifest=json.loads((directory/'manifest.json').read_text())
    if sha(directory/'forest.joblib')!=manifest['modelSha256'] or sha(directory/'training_provenance.json')!=manifest['datasetSha256']:
        raise ValueError('Existing model/data changed without a matching manifest')
    archive=directory/'versions'/(manifest['modelSha256'][:16]+'-'+manifest['datasetSha256'][:16])
    archive.mkdir(parents=True,exist_ok=True)
    for name in ['forest.joblib','training_provenance.json','manifest.json']:
        target=archive/name
        if not target.exists():
            shutil.copyfile(directory/name,target)


def reliability(validation, domain, context=None):
    """Fail closed for advisory use, not for displaying an actual computed SHAP.

    Accuracy is a prediction-error gate, never the SHAP additivity residual.
    An unverified workload/phase is NOT assumed to be the training domain.
    """
    context=context or {}
    reasons=[]
    error=validation.get('maePercentagePoints')
    if not isinstance(error,(float,int)) or not math.isfinite(error) or error>5:
        reasons.append('Independent prediction MAE exceeds the 5 pp advisory threshold or is unavailable.')
    if validation.get('scenarioGroups',0)<20 or validation.get('rows',0)<100:
        reasons.append('Validation coverage is too small for trusted tuning advice (requires 100 rows and 20 scenario groups).')
    if domain.get('outsideTrainingRangeFeatures'):
        reasons.append('Measured features exceed the training ranges; extrapolation is not validated.')
    if str(context.get('workload','')).lower()!='tpcc':
        reasons.append('Workload is not verified TPC-C; AP/TPC-DS and unknown workloads are unsupported.')
    if context.get('phase') not in ('pressure_before','pressure_after','pressure'):
        reasons.append('The capture is not verified to belong to a TPC-C pressure phase.')
    return {'status':'low_confidence' if reasons else 'experimental_supported',
            'decisionEligible':not reasons,'reasons':reasons,'maeThresholdPercentagePoints':5,
            'context':context,'policy':'Display actual attributions; exclude low-confidence numeric attributions from the LLM prompt.'}


def llm_evidence(result):
    """Never leak excluded predictions/contributions through a fallback prompt."""
    trust=result.get('reliability') or {}
    if trust.get('decisionEligible') is not True:
        return {'modelVersion':result.get('modelVersion'),'status':'excluded_low_confidence',
                'reliability':trust or {'status':'unverified','decisionEligible':False},
                'instruction':'Do not infer a tuning direction from SHAP; use measured perf, source evidence and workload replay.'}
    return {key:result.get(key) for key in ('method','modelVersion','explainedTarget','baseValue','prediction',
                                           'features','domain','validation','reliability','limitations')}


def explain(counts_path, model_dir=DEFAULT_MODEL, context=None):
    import joblib
    import numpy as np
    import shap

    started = time.monotonic()
    directory = Path(model_dir)
    manifest = json.loads((directory/'manifest.json').read_text())
    if sha(directory/'forest.joblib') != manifest['modelSha256'] or sha(directory/'training_provenance.json') != manifest['datasetSha256']:
        raise ValueError('Model/data digest mismatch; refusing unverified model deserialization')
    dataset = json.loads((directory/'training_provenance.json').read_text())
    independent=None
    external=directory/'external_validation.json'
    if external.is_file():
        report=json.loads(external.read_text())
        if report.get('sourceModelSha256')==manifest['modelSha256'] and report.get('sourceDatasetSha256')==manifest['datasetSha256']:
            independent={'method':report['method'],**report['validation'],'publishedAt':report['publishedAt'],
                         'evaluationSha256':report['evaluationSha256'],'reportSha256':sha(external),
                         'candidateValidation':report.get('candidateValidation'),'candidatePromoted':report.get('candidatePromoted'),
                         'limitations':report.get('limitations',[])}
    rates = read_rates(counts_path)
    names = manifest['featureNames']
    matrix = np.array([[rates.get(name,0.0) for name in names]])
    model = joblib.load(directory/'forest.joblib')  # Only the pinned, locally trained asset.
    # Explicit mode, no silent API-version-dependent choice. The fitted trees'
    # real training path sample counts define the background distribution.
    explainer = shap.TreeExplainer(model, feature_perturbation='tree_path_dependent', model_output='raw')
    values = np.asarray(explainer.shap_values(matrix, check_additivity=True)).reshape(-1)
    base = float(np.asarray(explainer.expected_value).reshape(-1)[0])
    prediction = float(model.predict(matrix)[0])
    residual = prediction - base - float(values.sum())
    if not math.isfinite(prediction + base + residual) or abs(residual)>1e-5:
        raise ValueError('SHAP additivity validation failed')
    mapping = associations()
    features = sorted([{'name':name,'value':float(matrix[0,i]),'shapValue':float(values[i]),
        'contributionPercentagePoints':float(values[i]*100),'impact':'increases_prediction' if values[i]>=0 else 'decreases_prediction',
        'parameters':sorted(mapping.get(name,[]))} for i,name in enumerate(names)], key=lambda r:abs(r['shapValue']),reverse=True)
    shown = features[:12]
    remainder = sum(r['shapValue'] for r in features[12:])
    if len(features)>12:
        shown.append({'name':'Other {} modeled functions'.format(len(features)-12),'value':None,
            'shapValue':remainder,'contributionPercentagePoints':remainder*100,'isAggregate':True,'parameters':[]})
    outside = [names[i] for i in range(len(names)) if matrix[0,i]<manifest['featureMin'][i] or matrix[0,i]>manifest['featureMax'][i]]
    domain={'outsideTrainingRangeFeatures':outside,'status':'outside_training_range' if outside else 'within_feature_ranges',
            'note':'Marginal feature ranges are a warning only, not proof of joint in-distribution coverage.'}
    return {'schemaVersion':SCHEMA,'status':'completed','method':'shap.TreeExplainer / tree_path_dependent TreeSHAP',
        'modelVersion':manifest['modelVersion'],'modelSha256':manifest['modelSha256'],
        'explainedTarget':manifest['target'],'outputUnit':manifest['outputUnit'],'featureUnit':manifest['featureUnit'],
        'baseValue':base,'prediction':prediction,'sumShapValues':float(values.sum()),'additivityResidual':residual,
        'features':shown,'allFeatures':features,'featureCount':len(features),'backgroundRows':len(dataset['records']),
        'backgroundMethod':'Actual fitted tree training-path sample counts (tree_path_dependent)',
        'validation':manifest['validation'],'modelScope':manifest['scope'],
        'domain':domain,'independentValidation':independent,
        'reliability':reliability(independent or manifest['validation'],domain,context),
        'input':{'path':str(Path(counts_path).resolve()),'sha256':sha(counts_path),'measuredFunctionCount':len(rates)},
        'trainingProvenanceSha256':manifest['datasetSha256'],'computedAt':dt.datetime.now(dt.timezone.utc).isoformat(),
        'computeSeconds':time.monotonic()-started,'limitations':manifest['limitations'],
        'usage':'Experimental model explanation; low-confidence attributions are excluded from LLM tuning advice. Never causal proof.'}


def explain_subprocess(counts_path, model_dir=None, context=None):
    """Keep SHAP/numpy/sklearn imports and memory away from bridge HTTP threads."""
    try:
        command = [sys.executable,str(Path(__file__).resolve()),'explain','--counts',str(counts_path),
                   '--model-dir',str(model_dir or os.environ.get('SYSINSIGHT_SHAP_MODEL_DIR',DEFAULT_MODEL)),
                   '--context-json',json.dumps(context or {})]
        env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMBA_NUM_THREADS='1')
        completed = subprocess.run(command,cwd=str(ROOT),env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
        if completed.returncode:
            raise RuntimeError(completed.stderr[-1500:] or completed.stdout[-1500:])
        return json.loads(completed.stdout)
    except Exception as exc:
        return {'schemaVersion':SCHEMA,'status':'unavailable','reason':'{}: {}'.format(type(exc).__name__,exc)[:1800],
                'features':[],'usage':'No attribution was calculated; never substitute differential rates for SHAP.'}


def compact_evidence(result):
    """Keep one full attribution artifact, not several copies in LLM/chain JSON."""
    summary = {key:value for key,value in result.items() if key != 'allFeatures'}
    if isinstance(summary.get('validation'),dict):
        summary['validation'] = {key:value for key,value in summary['validation'].items() if key!='folds'}
    summary['fullAttributionArtifact'] = 'shap_analysis.json'
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['train','explain'])
    parser.add_argument('--source',default=str(ROOT/'results/tpcc_api_validation'))
    parser.add_argument('--counts')
    parser.add_argument('--model-dir',default=str(DEFAULT_MODEL))
    parser.add_argument('--output')
    parser.add_argument('--context-json',default='{}')
    args=parser.parse_args()
    result=train(args.source,args.model_dir) if args.action=='train' else explain(args.counts,args.model_dir,json.loads(args.context_json))
    if args.output:
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    else:
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    sys.path.insert(0,str(ROOT/'.pydeps'))
    main()
