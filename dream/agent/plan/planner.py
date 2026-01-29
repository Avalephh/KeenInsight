import asyncio
import os
import sys

# Add RCRank to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
rcrank_path = os.path.join(current_dir, "RCRank")
if rcrank_path not in sys.path:
    sys.path.append(rcrank_path)

import json

import numpy as np
import pandas as pd
from agents import Agent, Runner
from model.modules.QueryFormer.utils import Encoding
from RCRank.utils.plan_encoding import PlanEncoder

from dream.agent.prompt import (
    build_root_cause_diagnosis_prompt,
    get_root_cause_llm_instructions,
)


class Planner:
    def __init__(self, memory_manager, predictor, agent_config):
        self.memory_manager = memory_manager
        self.predictor = predictor
        self.agent_config = agent_config
        self.device = predictor.device if predictor is not None else "cpu"

        # Initialize encoding components
        self.encoding = Encoding(None, {"NA": 0})

        self.root_cause_model = self.agent_config.get("root_cause_llm_model")

    def encode_plan(self, query_plan):
        df = pd.DataFrame([{"plan_json": (json.dumps({"Plan": query_plan}) if isinstance(query_plan, dict) else query_plan)}])

        plan_encoder = PlanEncoder(df=df, encoding=self.encoding)
        encoded_df = plan_encoder.df

        return encoded_df["json_plan_tensor"].iloc[0]

    def model_predict(self, query_info):
        query, plan, timeseries, log = self._build_prediction_input(query_info)

        pred_label_opt, pred_label = self.predictor.predict(query=query, plan=plan, timeseries=timeseries, log=log)

        result = self._extract_top_root_cause(pred_label, pred_label_opt)
        result["pred_label_opt"] = pred_label_opt.cpu().numpy()
        return result

    def _run_diagnosis_agent_sync(self, prompt):
        """Synchronous helper method to run diagnosis agent in thread pool"""
        try:
            return Runner.run_sync(
                starting_agent=Agent(
                    name="RootCauseLLM",
                    model=self.root_cause_model,
                    instructions=get_root_cause_llm_instructions(),
                ),
                input=prompt,
            ).final_output.strip()
        except RuntimeError as e:
            # If nested event loop issue, use nest_asyncio
            if "cannot be called" in str(e) or "event loop" in str(e).lower():
                import nest_asyncio
                nest_asyncio.apply()
                return Runner.run_sync(
                    starting_agent=Agent(
                        name="RootCauseLLM",
                        model=self.root_cause_model,
                        instructions=get_root_cause_llm_instructions(),
                    ),
                    input=prompt,
                ).final_output.strip()
            raise
    
    async def diagnosis_agent(self, prompt):
        """Async wrapper for diagnosis agent to work with event loop"""
        loop = asyncio.get_event_loop()
        # Run in executor to avoid nested event loop issues
        result = await loop.run_in_executor(
            None, 
            self._run_diagnosis_agent_sync, 
            prompt
        )
        return result

    async def llm_predict(self, query_info, root_causes, state_confidence, tuning_history=None):
        # ε-greedy retrieval structure: {"positive", "negative"}
        # retrieval = self.memory_manager.retrieve_cases(query_info, root_causes)
        # positives = retrieval.get('positive', [])
        # negatives = retrieval.get('negative', [])

        prompt = build_root_cause_diagnosis_prompt(
            query_info,
            root_causes,
            state_confidence=state_confidence,
            tuning_history=tuning_history,
        )
        # print("prompt: ", prompt)

        output = await self.diagnosis_agent(prompt)
        parsed = self.parse_and_validate(output)
        if parsed is not None:
            labels, conf_map, explanation = parsed
        else:
            labels, conf_map, explanation = None, None, ""
        # retry
        retry = 0
        while labels is None and retry < 5:
            output = await self.diagnosis_agent(prompt)
            parsed = self.parse_and_validate(output)
            if parsed is not None:
                labels, conf_map, explanation = parsed
            else:
                labels, conf_map, explanation = None, None, ""
            retry += 1

        if labels is None:
            labels = ["normal"]
            conf_map = {"normal": 1.0}
            explanation = "Unable to diagnose root causes. Defaulting to normal state."

        return {"root_causes": labels, "confidence": conf_map, "explanation": explanation}

    def parse_and_validate(self, json_text):
        try:
            data = json.loads(json_text)
            reported_pred = [str(x).lower().strip() for x in data.get("predicted_root_causes", [])]
            items = data.get("root_causes", [])
            explanation = data.get("explanation", "")

            derived_pred = []
            conf_map = {}
            for it in items:
                label = str(it.get("label", "")).lower().strip()
                conf = float(it.get("confidence", 0.0))
                if conf > 0.5:
                    derived_pred.append(label)
                conf_map[label] = conf

            # check
            if set(reported_pred) != set(derived_pred):
                return None

            if len(derived_pred) == 0:
                derived_pred = ["normal"]
                conf_map = {"normal": 1.0}

            return derived_pred, conf_map, explanation
        except Exception:
            return None

    def _build_prediction_input(self, query_info):

        query = query_info.query if hasattr(query_info, "query") else ""
        plan = self.encode_plan(query_info.plan_json)
        # timeseries = json.loads(query_info.external_metrics)
        # log = json.loads(query_info.internal_metrics)
        timeseries = query_info.external_metrics
        log = query_info.internal_metrics

        return query, plan, timeseries, log

    def _extract_top_root_cause(self, pred_label, pred_label_opt):
        pred_label = pred_label.cpu().numpy()
        pred_label_opt = pred_label_opt.cpu().numpy()

        # issue type mapping
        issue_types = {
            0: "missing indexes",
            1: "suboptimal plan optimizer",
            2: "inappropriate query knobs",
            3: "poorly written queries",
            4: "normal",
        }

        print("pred_label: ", pred_label)
        print("pred_label_opt: ", pred_label_opt)

        if pred_label.sum() == 0:
            return {"root_causes": "normal"}

        predicted_classes = np.where(pred_label[0] == 1)[0]
        if len(predicted_classes) == 0:
            return {"root_causes": "normal"}
            # return {"root_cause": "normal", "confidence": pred_label_opt}

        root_causes = []
        root_causes_confidence = []
        for class_idx in predicted_classes:
            root_cause = issue_types.get(class_idx)
            if root_cause not in root_causes:
                root_causes.append(root_cause)
                root_causes_confidence.append(pred_label_opt[0][class_idx])

        return {"root_causes": root_causes}
        # root_cause = {
        #     "root_cause": root_causes,
        #     "confidence": root_causes_confidence
        # }

    async def predict(self, query_info, state, memory_manager):
        # If current_root already exists, reuse it; otherwise call small model + LLM to get new root cause
        current_root = state.get("current_root")
        if current_root is None:
            # Small model initial prediction + confidence
            model_out = self.model_predict(query_info)
            current_root = model_out.get("root_causes")
            print("Small model predicted root cause: ", current_root)
            pred_opt = model_out.get("pred_label_opt")
            labels = [
                "missing indexes",
                "suboptimal plan optimizer",
                "inappropriate query knobs",
                "poorly written queries",
            ]
            confidence = np.array(pred_opt).reshape(-1)
            state["confidence"] = {labels[i]: float(confidence[i]) for i in range(confidence.shape[0])}

        # Pass current root cause and confidence to LLM
        # Build historical success/failure counts aggregated by root cause
        tuning_history = []
        union_keys = set(state.get("attempts", {}).keys()) | set(state.get("successes", {}).keys())
        for k in union_keys:
            # Normalize to frozenset or single element set
            label = list(k)
            tuning_history.append(
                {
                    "label": label,
                    "success_count": int(state.get("successes", {}).get(k, 0)),
                    "fail_count": int(state.get("attempts", {}).get(k, 0)),
                }
            )

        llm_result = await self.llm_predict(
            query_info,
            current_root,
            state_confidence=state.get("confidence"),
            tuning_history=tuning_history,
        )
        predicted_root = llm_result.get("root_causes")
        state["confidence"] = llm_result.get("confidence")
        explanation = llm_result.get("explanation", "")

        print("predicted_root: ", predicted_root)
        print("state: ", state)
        print("explanation: ", explanation)

        return predicted_root, state, explanation
