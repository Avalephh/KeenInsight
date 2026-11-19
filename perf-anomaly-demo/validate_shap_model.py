#!/usr/bin/env python3
"""Select on historical grouped folds, then validate on an untouched time batch.

prepare must run before examining the last collection batch. No artificial TPS
labels, in-sample accuracy claims or silent production-model replacements.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

from sysinsight_shap import DEFAULT_MODEL, sha, read_rates, archive_model_assets


def run(args):
    import joblib
    import numpy as np
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.metrics import mean_absolute_error, r2_score
    from sklearn.model_selection import GroupKFold

    dest=Path(args.output);dest.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((DEFAULT_MODEL/'manifest.json').read_text())
    data=json.loads((DEFAULT_MODEL/'training_provenance.json').read_text())
    if args.action=='publish':
        report=json.loads((dest/'evaluation.json').read_text())
        protocol=json.loads((dest/'protocol.json').read_text())
        if protocol['historicalDatasetSha256']!=manifest['datasetSha256'] or report['protocolSha256']!=sha(dest/'protocol.json'):
            raise ValueError('Independent validation is for a different model/dataset protocol')
        value={'sourceModelSha256':manifest['modelSha256'],'sourceDatasetSha256':manifest['datasetSha256'],
               'method':'Independent later time batch, six existing scenarios; not used to train the pinned model',
               'validation':{**report['legacy'],'rows':report['holdoutRows'],'scenarioGroups':len(report['testRows'])},
               'candidateValidation':report['candidate'],'candidatePromoted':False,
               'evaluationSha256':sha(dest/'evaluation.json'),'protocolSha256':report['protocolSha256'],
               'testRows':report['testRows'],'limitations':report['limitations'],
               'publishedAt':dt.datetime.now(dt.timezone.utc).isoformat()}
        serialized=json.dumps(value,indent=2)
        (DEFAULT_MODEL/'external_validation.json').write_text(serialized)
        archive=DEFAULT_MODEL/'external-validations';archive.mkdir(exist_ok=True)
        (archive/(sha(DEFAULT_MODEL/'external_validation.json')+'.json')).write_text(serialized)
        print(json.dumps(value));return
    names=manifest['featureNames']
    x=np.array([r['values'] for r in data['records']]);y=np.array([r['target'] for r in data['records']])
    groups=np.array([r['group'] for r in data['records']])
    def forest():
        return RandomForestRegressor(n_estimators=96,max_depth=5,min_samples_leaf=2,max_features=.5,n_jobs=1,random_state=20261023)
    def fit(a,b,k):
        selector=forest().fit(a,b)
        columns=np.arange(a.shape[1]) if not k else np.sort(np.argsort(selector.feature_importances_)[-k:])
        return forest().fit(a[:,columns],b),columns
    if args.action=='prepare':
        if (dest/'protocol.json').exists():raise ValueError('Protocol already frozen; do not overwrite selection after seeing test data')
        # Fixed candidates; each fold selects features using training labels ONLY.
        scores=[]
        for k in (0,32,64):
            pred=np.zeros(len(y));folds=[]
            for train,test in GroupKFold(5).split(x,y,groups):
                model,columns=fit(x[train],y[train],k)
                pred[test]=model.predict(x[test][:,columns])
                folds.append({'trainingGroups':sorted(set(groups[train])),'heldOutGroups':sorted(set(groups[test]))})
            scores.append({'selectedFeatures':k or len(names),'selectorLimit':k,'maePercentagePoints':float(mean_absolute_error(y,pred)*100),
                           'r2':float(r2_score(y,pred)),'folds':folds})
        best=min(scores,key=lambda s:s['maePercentagePoints'])
        protocol={'frozenAt':dt.datetime.now(dt.timezone.utc).isoformat(),'historicalDatasetSha256':manifest['datasetSha256'],
                  'developmentScores':scores,'selected':best,'testBatch':3,
                  'trainingPolicy':'Historical 53 profiles plus complete collection batches 1 and 2; all rows equal weight.',
                  'testPolicy':'Collection batch 3 is never used for training or candidate selection; evaluate once against pinned legacy model.',
                  'promotionRule':'Held-out MAE at least 10% lower than pinned legacy, nonnegative held-out R2; no automatic promotion.',
                  'accuracyGatePercentagePoints':5.0}
        (dest/'protocol.json').write_text(json.dumps(protocol,indent=2))
        print(json.dumps(protocol));return
    protocol=json.loads((dest/'protocol.json').read_text())
    if (dest/'evaluation.json').exists():raise ValueError('Immutable holdout evaluation already exists; do not reselect against test results')
    audit=json.loads(Path(args.dataset).read_text())
    if audit.get('failures') or len(audit['rows'])!=18:raise ValueError('Need all 18 genuine completed measurements')
    measured=[]
    seen=set()
    for row in audit['rows']:
        if sha(row['countsPath'])!=row['countsSha256'] or row['countsSha256'] in seen:raise ValueError('Profile digest mismatch or duplicate')
        seen.add(row['countsSha256']);rates=read_rates(row['countsPath'])
        from collect_shap_windows import aligned_tps
        for phase in ('baseline','pressure'):
            logfile=Path(row['directory'])/phase/'control.log'
            if sha(logfile)!=row[phase]['logSha256']:raise ValueError('Foreground progress log digest mismatch')
            recomputed=aligned_tps(logfile,*row[phase]['windowSeconds'])
            if abs(recomputed['tps']-row[phase]['tps'])>1e-8:raise ValueError('Window TPS does not match the actual foreground log')
        if row['baseline']['coveredSeconds']<4 or row['pressure']['coveredSeconds']<4:raise ValueError('Missing aligned TPS windows')
        expected=row['pressure']['tps']/row['baseline']['tps']
        if abs(expected-row['target'])>1e-10:raise ValueError('Target differs from real TPS pair')
        if row['startedAt']<=protocol['frozenAt'] and row['batch']==3:raise ValueError('Holdout began before protocol freeze')
        measured.append((row,np.array([rates.get(n,0.) for n in names])))
    development=[item for item in measured if item[0]['batch']!=3]
    test=[item for item in measured if item[0]['batch']==3]
    trainx=np.vstack([x]+[v[None,:] for r,v in development]);trainy=np.concatenate([y,[r['target'] for r,v in development]])
    testx=np.vstack([v for r,v in test]);testy=np.array([r['target'] for r,v in test])
    candidate,columns=fit(trainx,trainy,protocol['selected']['selectorLimit'])
    legacy=joblib.load(DEFAULT_MODEL/'forest.joblib')
    oldpred=legacy.predict(testx);newpred=candidate.predict(testx[:,columns])
    def metrics(pred):return {'maePercentagePoints':float(mean_absolute_error(testy,pred)*100),'r2':float(r2_score(testy,pred))}
    oldscore,newscore=metrics(oldpred),metrics(newpred)
    eligible=newscore['maePercentagePoints']<=oldscore['maePercentagePoints']*.9 and newscore['r2']>=0
    report={'protocolSha256':sha(dest/'protocol.json'),'legacy':oldscore,'candidate':newscore,'eligibleForPromotion':eligible,
            'holdoutRows':len(test),'holdoutBatches':[3],'developmentNewRows':len(development),'trainingRows':len(trainy),
            'testRows':[{'scenarioId':r['scenarioId'],'countsSha256':r['countsSha256'],'target':float(testy[i]),
                        'legacyPrediction':float(oldpred[i]),'candidatePrediction':float(newpred[i])} for i,(r,v) in enumerate(test)],
            'limitations':['Only one independent time batch with six scenario measurements; confidence remains limited.',
                           'Historical and newly aligned target windows differ; domain shift is explicitly tested, not concealed.']}
    modeldir=dest/'candidate';modeldir.mkdir(exist_ok=True)
    joblib.dump(candidate,modeldir/'forest.joblib',compress=3)
    selectednames=[names[i] for i in columns]
    trainrecords=[{**r,'values':[r['values'][i] for i in columns]} for r in data['records']]
    for row,values in development:
        trainrecords.append({'group':row['scenarioId'],'target':row['target'],'values':values[columns].tolist(),
                             'profile':row['countsPath'],'profileSha256':row['countsSha256'],
                             'baselineControlTps':row['baseline']['tps'],'pressureControlTps':row['pressure']['tps'],
                             'foregroundWorker':row['pressure']['workerApplication'],'alignedMeasurement':row})
    (modeldir/'training_provenance.json').write_text(json.dumps({'schemaVersion':data['schemaVersion'],'featureNames':selectednames,'records':trainrecords},indent=2))
    newmanifest={**manifest,'modelVersion':'pg12-foreground-tps-'+sha(modeldir/'forest.joblib')[:16],
                 'createdAt':dt.datetime.now(dt.timezone.utc).isoformat(),'featureNames':selectednames,
                 'featureMin':trainx[:,columns].min(axis=0).tolist(),'featureMax':trainx[:,columns].max(axis=0).tolist(),
                 'targetMin':float(trainy.min()),'targetMax':float(trainy.max()),
                 'modelSha256':sha(modeldir/'forest.joblib'),'datasetSha256':sha(modeldir/'training_provenance.json'),
                 'validation':{'method':'Frozen historical grouped development; independent final time batch (not training data)',
                               **newscore,'rows':len(test),'scenarioGroups':len(test),'trainingRows':len(trainy),
                               'development':protocol['selected'],'holdout':report},
                 'selectionProtocolSha256':report['protocolSha256'],
                 'limitations':manifest['limitations']+report['limitations']}
    (modeldir/'manifest.json').write_text(json.dumps(newmanifest,indent=2))
    archive_model_assets(modeldir)
    (dest/'evaluation.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','evaluate','publish']);p.add_argument('--output',required=True);p.add_argument('--dataset')
    run(p.parse_args())
