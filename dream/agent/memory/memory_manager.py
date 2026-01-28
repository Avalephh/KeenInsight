import itertools
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional, Set

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from numpy.linalg import norm

# Import soft Q retriever
from dream.agent.memory.soft_q_retriever import SoftQRetriever
from dream.agent.plan.RCRank.utils.load_data import (
    load_dataset_valid as load_dataset_tensor_valid,
)
from dream.utils.types import CaseInfo, QueryInfo

logger = logging.getLogger(__name__)

# Ensure logger inherits root logger configuration
logger.setLevel(logging.INFO)
logger.propagate = True


@dataclass
class MemoryCase:
    id: str
    case_info: CaseInfo
    timestamp: float = field(default_factory=time.time)
    metrics: dict = field(default_factory=dict)
    sql_embedding: Optional[List[float]] = None
    action_embedding: Optional[List[float]] = None


class MemoryManager:
    def __init__(self, config, predictor=None, planner_config=None):
        self.db_path = config.get("db_path")
        self.archive_size = config.get("archive_size")
        self.cases = {}
        self.archive = set()
        self.best_case_id = None

        self.enable_finetune = config.get("enable_finetune")
        self.finetune_threshold = config.get("finetune_threshold")
        self.finetune_epochs = config.get("finetune_epochs")
        self.finetune_batch_size = config.get("finetune_batch_size")
        self.finetune_device = config.get("finetune_device")
        self.model_output_dir = config.get("model_output_dir")

        self.bce_weight = config.get("bce_weight")
        self.contrastive_weight = config.get("contrastive_weight")
        self.lambda_neg = config.get("lambda_neg", 1.0)
        
        # Exploration threshold for verified supervision
        self.eta = config.get("eta", 3)  # Minimum exploration threshold η
        
        # Contrastive loss negative pair weights: λ_4 < λ_3 < λ_2 < λ_1
        self.lambda_1 = config.get("lambda_1", 1.0)  # N1: uncertain negatives (insufficient exploration)
        self.lambda_2 = config.get("lambda_2", 0.8)   # N2: verified negatives (sufficient exploration)
        self.lambda_3 = config.get("lambda_3", 0.5)   # N3: verified negatives (different bottlenecks)
        self.lambda_4 = config.get("lambda_4", 0.2)   # N4: remaining pairs

        self.bert_model_path = config.get("bert_model_path")

        # Retrieval mode configuration: static or dynamic
        self.retrieval_mode = config.get("retrieval_mode", "dynamic")

        # Sample save configuration
        self.enable_save_samples = config.get("enable_save_samples", False)
        self.samples_save_path = config.get("samples_save_path")

        # Retriever model save/load path
        self.retriever_model_path = config.get("retriever_model_path")

        # Track pending experiences (experiences without next_query_info) by query_id
        # Format: {query_id: {"line_number": int, "exp_dict": dict}}
        self.pending_experiences = {}

        self.root_cause_types = [
            "missing indexes",
            "inappropriate query knobs",
            "suboptimal plan optimizer",
            "poorly written queries",
        ]
        # Support multiple root cause combinations
        self.island_keys = self._generate_island_keys(self.root_cause_types)
        self.islands = {key: set() for key in self.island_keys}

        # Create island for "normal" root cause as well
        normal_island_key = self._normalize_root_causes(["normal"])
        self.islands[normal_island_key] = set()

        self._rcrank_predictor = predictor
        self._planner_config = planner_config  # Store planner config for lazy initialization

        # Initialize soft Q retriever
        self.retriever = SoftQRetriever(memory_manager=self)

        # Load the saved retriever model
        if os.path.exists(self.retriever_model_path):
            self.retriever.load_model(self.retriever_model_path)

        self._init_database()
        self.load()
        
        # Load pending experiences from file if it exists
        self._load_pending_experiences()

    def _cases_to_dataframe(self):
        rows = []
        label_space = self.root_cause_types

        for cid, case in self.cases.items():
            info = case.case_info
            raw = info.get("query_info", {})
            query = raw.get("query", "")
            plan_json = raw.get("plan_json")

            # Unify to stringified plan JSON
            if isinstance(plan_json, dict):
                plan_json = json.dumps({"Plan": plan_json})
            external_metrics = raw.get("external_metrics", [])
            internal_metrics = raw.get("internal_metrics", [])

            roots = info.get("root_causes") or []
            roots = roots if isinstance(roots, list) else [roots]
            label_vec = [1 if r in roots else 0 for r in label_space]

            duration = info.get("new_time", 0.0)
            
            # Get tuning attempts per component: n_{i,b}
            # tuning_attempts is a dict mapping component (root_cause_type) to number of attempts
            tuning_attempts = info.get("tuning_attempts", {})
            if not tuning_attempts or not isinstance(tuning_attempts, dict):
                # Fallback: if positive case, assume at least 1 attempt per root cause component
                # For negative cases, assume 0 attempts (unverified)
                tuning_attempts = {}
                if info.get("label") == "positive":
                    for rc in roots:
                        if rc in label_space:
                            tuning_attempts[rc] = tuning_attempts.get(rc, 0) + 1
            
            # Build attempts vector: [n_{i,b}] for each component b in label_space
            attempts_vec = [tuning_attempts.get(rc, 0) for rc in label_space]
            
            rows.append(
                {
                    "sql_file": self.db_path,
                    "query": query,
                    "plan_json": plan_json,
                    "internal_metrics": json.dumps(internal_metrics),
                    "external_metrics": json.dumps(external_metrics),
                    "multilabel": str(label_vec),
                    "duration": duration,
                    "error": None,
                    "case_label": info.get("label", "positive"),
                    "tuning_attempts": json.dumps(attempts_vec),  # n_{i,b} for each component b
                }
            )
        return pd.DataFrame(rows)

    def _export_cases_csv(self, csv_path):
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        df = self._cases_to_dataframe()
        df.to_csv(csv_path, index=False)
        return csv_path

    def _run_finetune(
        self,
        csv_path,
        output_dir,
        device,
        epochs,
        batch_size,
        bce_weight=0.7,
        contrastive_weight=0.3,
        lambda_neg=1.0,
    ):
        # Use the hybrid loss (Masked BCE + enhanced contrastive learning) to finetune the RCRank model
        # According to paper: L_total = λ_b * L_b + λ_c * L_c
        os.makedirs(output_dir, exist_ok=True)

        train_dataloader, _, _, train_len, _, _, _ = load_dataset_tensor_valid(csv_path, batch_size=batch_size, device=device)

        if not hasattr(self, "_rcrank_predictor") or self._rcrank_predictor is None:
            from agent.plan.online_predict import RCRankPredictor

            self._rcrank_predictor = RCRankPredictor(device=device, train_data_path=csv_path)
        predictor = self._rcrank_predictor
        model = predictor.model.to(device)
        model.train()
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-4)  # Important parameter, try 1e-4 or 3e-4
        temperature = 0.07
        eta = self.eta  # Minimum exploration threshold

        def masked_bce_loss(pred_label, labels, case_labels, tuning_attempts, eta, device):
            """
            Masked Binary Cross-Entropy Loss according to paper:
            L_b(B) = -1/(sum m_{i,b}) * sum_{i,b} m_{i,b} [Y_{i,b}*log(Ŷ_{i,b}) + (1-Y_{i,b})*log(1-Ŷ_{i,b})]
            
            Supervision mask m_{i,b}:
            - If case_label is "positive": m_{i,b} = 1 for all components (entire label vector is kept)
            - If case_label is "negative": 
              - m_{i,b} = 1 if n_{i,b} >= η (verified negative, keep this component)
              - m_{i,b} = 0 if n_{i,b} < η (unverified negative, mask this component)
            
            Args:
                pred_label: [B, K] - predicted probabilities
                labels: [B, K] - true labels (multi-label)
                case_labels: [B] - case labels ("positive" or "negative")
                tuning_attempts: [B, K] - number of tuning attempts n_{i,b} for each component
                eta: minimum exploration threshold η
                device: device for tensors
            """
            batch_size = pred_label.size(0)
            num_components = pred_label.size(1)
            
            # Convert case_labels to boolean tensor
            is_positive_case = torch.tensor(
                [label == "positive" for label in case_labels],
                device=device,
                dtype=torch.bool
            )  # [B]
            
            # Initialize supervision mask
            supervision_mask = torch.zeros_like(pred_label, dtype=torch.bool, device=device)  # [B, K]
            
            # For positive cases: all components are kept (m_{i,b} = 1 for all b)
            for i in range(batch_size):
                if is_positive_case[i]:
                    supervision_mask[i, :] = True
                else:
                    # For negative cases: keep only components where n_{i,b} >= η
                    supervision_mask[i, :] = tuning_attempts[i, :] >= eta
            
            # If no valid supervision, return zero loss
            if not supervision_mask.any():
                return torch.tensor(0.0, device=device, requires_grad=True)
            
            # Compute BCE loss only for masked positions
            bce_per_element = -(
                labels * torch.log(pred_label + 1e-12) + 
                (1 - labels) * torch.log(1 - pred_label + 1e-12)
            )
            
            # Apply mask and compute mean
            masked_bce = bce_per_element * supervision_mask.float()
            total_mask_sum = supervision_mask.float().sum()
            
            if total_mask_sum > 0:
                loss = masked_bce.sum() / total_mask_sum
            else:
                loss = torch.tensor(0.0, device=device, requires_grad=True)
            
            return loss

        def enhanced_contrastive_loss(emb, labels, case_labels, tuning_attempts, temperature, eta, lambda_1, lambda_2, lambda_3, lambda_4, device):
            """
            Enhanced contrastive learning loss according to paper:
            L_c(B) = -1/|I_B| * sum_{i in I_B} (1/|P(i)|) * sum_{j in P(i)} log(exp(s_ij/τ) / sum_{t=1}^4 sum_{k in N_t(i)} λ_t * exp(s_ik/τ))
            
            Positive pairs P(i): {j | Y_j=1, B_i=B_j} (both positive, same bottleneck)
            
            Negative pairs:
            - N1(i): {j | Y_j=0, B_i=B_j, n_j < η} (uncertain negatives, insufficient exploration)
            - N2(i): {j | Y_j=0, B_i=B_j, n_j >= η} (verified negatives, sufficient exploration)
            - N3(i): {j | Y_j=1, B_i∩B_j=∅} (verified negatives, different bottlenecks)
            - N4(i): I_B \ (P(i) ∪ N1(i) ∪ N2(i) ∪ N3(i)) (remaining pairs)
            
            Weights: λ_4 < λ_3 < λ_2 < λ_1
            
            Args:
                emb: [B, D] - sample embeddings
                labels: [B, K] - multi-label one-hot vectors (root cause labels)
                case_labels: [B] - case labels ("positive" or "negative")
                tuning_attempts: [B, K] - number of tuning attempts n_{i,b}
                temperature: temperature parameter τ
                eta: minimum exploration threshold η
                lambda_1, lambda_2, lambda_3, lambda_4: weights for negative pair types
                device: device for tensors
            """
            batch_size = emb.size(0)
            emb = F.normalize(emb, dim=1)
            
            # Compute similarity matrix: s_ij = (e_i^T e_j) / (||e_i|| ||e_j||)
            # Since embeddings are normalized, s_ij = e_i^T e_j
            similarity_matrix = torch.matmul(emb, emb.T)  # [B, B]
            
            # Scale by temperature: s_ij / τ
            logits = similarity_matrix / temperature  # [B, B]
            
            # Mask: exclude self
            self_mask = torch.eye(batch_size, dtype=torch.bool, device=device)
            logits_mask = ~self_mask
            
            # Stabilization: subtract max for numerical stability
            logits_max, _ = torch.max(logits.masked_fill(~logits_mask, float("-inf")), dim=1, keepdim=True)
            logits = logits - logits_max.detach()
            exp_logits = torch.exp(logits) * logits_mask.float()  # [B, B]
            
            # Convert case_labels to boolean tensor
            is_positive = torch.tensor(
                [label == "positive" for label in case_labels], 
                device=device, 
                dtype=torch.bool
            )  # [B]
            
            # Compute bottleneck sets for each sample
            # B_i is the set of components where labels[i, b] = 1
            bottleneck_sets = labels > 0.5  # [B, K] boolean
            
            total_loss = torch.tensor(0.0, device=device, requires_grad=True)
            valid_anchors = 0
            
            for i in range(batch_size):
                # Only compute loss for positive anchors
                if not is_positive[i]:
                    continue
                
                valid_anchors += 1
                B_i = bottleneck_sets[i]  # [K] boolean vector
                
                # Build positive pairs P(i): {j | Y_j=1, B_i=B_j}
                P_i = []
                for j in range(batch_size):
                    if i == j:
                        continue
                    if is_positive[j]:
                        B_j = bottleneck_sets[j]
                        # Check if B_i = B_j (same bottleneck set)
                        if torch.equal(B_i, B_j):
                            P_i.append(j)
                
                if len(P_i) == 0:
                    continue
                
                # Build negative pairs
                N1_i = []  # {j | Y_j=0, B_i=B_j, n_j < η}
                N2_i = []  # {j | Y_j=0, B_i=B_j, n_j >= η}
                N3_i = []  # {j | Y_j=1, B_i∩B_j=∅}
                N4_i = []  # remaining pairs
                
                for j in range(batch_size):
                    if i == j:
                        continue
                    
                    B_j = bottleneck_sets[j]
                    # Check if B_i = B_j (same bottleneck set)
                    same_bottleneck = torch.equal(B_i, B_j)
                    # Check if B_i ∩ B_j = ∅ (no shared components)
                    no_intersection = (B_i & B_j).sum() == 0
                    
                    if not is_positive[j] and same_bottleneck:
                        # N1: {j | Y_j=0, B_i=B_j, n_j < η}
                        # N2: {j | Y_j=0, B_i=B_j, n_j >= η}
                        # Check exploration threshold: for all b in B_i, check if n_{j,b} >= η
                        # Get attempts for components in B_i
                        attempts_for_B_i = tuning_attempts[j][B_i]
                        # If all components in B_i have attempts >= eta, it's N2, else N1
                        if (attempts_for_B_i >= eta).all():
                            N2_i.append(j)
                        else:
                            N1_i.append(j)
                    elif is_positive[j] and no_intersection:
                        N3_i.append(j)
                    else:
                        # Remaining pairs
                        N4_i.append(j)
                
                # Compute denominator: sum_{t=1}^4 sum_{k in N_t(i)} λ_t * exp(s_ik/τ)
                denominator = torch.tensor(0.0, device=device)
                for k in N1_i:
                    denominator = denominator + lambda_1 * exp_logits[i, k]
                for k in N2_i:
                    denominator = denominator + lambda_2 * exp_logits[i, k]
                for k in N3_i:
                    denominator = denominator + lambda_3 * exp_logits[i, k]
                for k in N4_i:
                    denominator = denominator + lambda_4 * exp_logits[i, k]
                
                # Add small epsilon for numerical stability
                denominator = denominator + 1e-12
                
                # Compute loss for each positive pair
                pair_loss = torch.tensor(0.0, device=device, requires_grad=True)
                for j in P_i:
                    numerator = exp_logits[i, j]
                    pair_loss = pair_loss + (-torch.log(numerator / denominator))
                
                # Average over positive pairs
                if len(P_i) > 0:
                    total_loss = total_loss + (pair_loss / len(P_i))
            
            # Average over valid anchors
            if valid_anchors > 0:
                return total_loss / valid_anchors
            else:
                return torch.tensor(0.0, device=device, requires_grad=True)

        def hybrid_loss(
            emb,
            labels,
            pred_label,
            case_labels,
            tuning_attempts,
            bce_weight=0.7,
            contrastive_weight=0.3,
            temperature=0.07,
            eta=3,
            lambda_1=1.0,
            lambda_2=0.8,
            lambda_3=0.5,
            lambda_4=0.2,
        ):
            """
            Hybrid loss according to paper: L(B) = λ_b * L_b(B) + λ_c * L_c(B)
            
            Where:
            - L_b: Masked Binary Cross-Entropy loss with supervision mask
            - L_c: Enhanced contrastive learning loss with 4 types of negative pairs
            """
            device = pred_label.device
            
            # 1. Masked BCE classification loss
            bce_loss = masked_bce_loss(pred_label, labels, case_labels, tuning_attempts, eta, device)

            # 2. Enhanced contrastive learning loss
            contrastive_loss = enhanced_contrastive_loss(
                emb, labels, case_labels, tuning_attempts, 
                temperature, eta, lambda_1, lambda_2, lambda_3, lambda_4, device
            )

            # 3. Weighted combination: L_total = λ_b * L_b + λ_c * L_c
            total_loss = bce_weight * bce_loss + contrastive_weight * contrastive_loss

            return total_loss, bce_loss, contrastive_loss

        # 3) Training loop
        print("Starting finetune")
        for epoch_idx in range(epochs):
            epoch_total_loss = 0.0
            epoch_bce_loss = 0.0
            epoch_contrastive_loss = 0.0

            print(f"Finetune epoch {epoch_idx}")
            for batch in train_dataloader:
                # batch keys: query(list[str]), plan(dict of tensors), log[tensor], timeseries[tensor], multilabel[tensor]
                sql_texts = batch["query"]
                plan = batch["plan"]
                logs = batch["log"].to(device)
                timeseries = batch["timeseries"].to(device)
                labels = batch["multilabel"].to(device).float()

                # Get case_labels (positive/negative labels)
                # Get case_label information from batch
                if "case_label" in batch:
                    case_labels = batch["case_label"]
                else:
                    # If no case_label column, use default value
                    case_labels = ["positive"] * len(sql_texts)
                
                # Get tuning_attempts n_{i,b} for each component
                if "tuning_attempts" in batch:
                    # Parse JSON string if needed
                    tuning_attempts_list = batch["tuning_attempts"]
                    if isinstance(tuning_attempts_list[0], str):
                        tuning_attempts_list = [json.loads(ta) for ta in tuning_attempts_list]
                    tuning_attempts = torch.tensor(tuning_attempts_list, device=device, dtype=torch.float32)  # [B, K]
                else:
                    # Default fallback when tuning_attempts are not available
                    # For positive labels: assume at least 1 attempt (verified)
                    # For negative labels with positive case_label: assume at least 1 attempt (tried but failed)
                    # For negative labels with negative case_label: assume 0 attempts (unverified)
                    default_attempts = labels.clone()  # Start with labels as base
                    for i, case_label in enumerate(case_labels):
                        if case_label == "positive" and labels[i].sum() == 0:
                            # Positive case but no positive labels - assume at least 1 attempt per component
                            default_attempts[i] = torch.ones_like(default_attempts[i])
                    tuning_attempts = default_attempts.to(device)

                # Text encoding
                sql = predictor.tokenizer(
                    sql_texts,
                    return_tensors="pt",
                    padding=True,
                    truncation=True,
                    max_length=512,
                ).to(device)
                # Plan tensor preparation
                plan = {
                    "x": plan["x"].squeeze(1).to(device).to(torch.float32),
                    "attn_bias": plan["attn_bias"].squeeze(1).to(device),
                    "rel_pos": plan["rel_pos"].squeeze(1).to(device),
                    "heights": plan["heights"].squeeze(1).to(device),
                }

                optimizer.zero_grad()
                with torch.set_grad_enabled(True):
                    # Always use hybrid loss
                    emb = model.get_embedding(sql, plan, timeseries, logs)  # [B, D]
                    pred_label, _ = model(sql, plan, timeseries, logs)  # [B, K]
                    total_loss, bce_loss, contrastive_loss = hybrid_loss(
                        emb,
                        labels,
                        pred_label,
                        case_labels,
                        tuning_attempts,
                        bce_weight,
                        contrastive_weight,
                        temperature,
                        eta,
                        self.lambda_1,
                        self.lambda_2,
                        self.lambda_3,
                        self.lambda_4,
                    )
                    epoch_bce_loss += bce_loss.item()
                    epoch_contrastive_loss += contrastive_loss.item()

                    total_loss.backward()
                    optimizer.step()
                    epoch_total_loss += total_loss.item()

            # Record training logs
            avg_total_loss = epoch_total_loss / max(1, len(train_dataloader))
            avg_bce_loss = epoch_bce_loss / max(1, len(train_dataloader))
            avg_contrastive_loss = epoch_contrastive_loss / max(1, len(train_dataloader))

            print(f"Hybrid finetune epoch {epoch_idx}: total_loss={avg_total_loss:.6f}, " f"bce_loss={avg_bce_loss:.6f}, contrastive_loss={avg_contrastive_loss:.6f}")

        # 4) Save weights
        best_path = os.path.join(output_dir, "best_model.pt")
        torch.save(model.state_dict(), best_path)
        return best_path

    def _reload_predictor(self, model_path, stats_data_path, device):
        if not hasattr(self, "_rcrank_predictor") or self._rcrank_predictor is None:
            from agent.plan.online_predict import RCRankPredictor

            self._rcrank_predictor = RCRankPredictor(model_path=model_path, train_data_path=stats_data_path, device=device)
            self._rcrank_predictor.model.eval()
            return

        predictor = self._rcrank_predictor
        predictor.device = device
        predictor.model_path = model_path
        predictor._compute_statistics(stats_data_path)
        predictor.model.load_state_dict(torch.load(model_path, map_location=device))
        predictor.model.to(device)
        predictor.model.eval()

    def _maybe_finetune(self):
        if not self.enable_finetune:
            return

        total = len(self.cases)
        if total - self._last_finetune_count < self.finetune_threshold:
            return

        # export CSV
        csv_path = os.path.join(self.model_output_dir, "memory_cases.csv")
        os.makedirs(self.model_output_dir, exist_ok=True)
        self._export_cases_csv(csv_path)

        # finetune
        best_model = self._run_finetune(
            csv_path,
            self.model_output_dir,
            self.finetune_device,
            self.finetune_epochs,
            self.finetune_batch_size,
            self.bce_weight,
            self.contrastive_weight,
            self.lambda_neg,
        )

        # reload predictor
        self._reload_predictor(best_model, csv_path, self.finetune_device)
        self._last_finetune_count = total

    def _init_database(self):
        if not os.path.exists(self.db_path):
            with open(self.db_path, "w") as f:
                json.dump({}, f)

    def _generate_island_keys(self, root_cause_types):
        keys = []
        for i in range(1, len(root_cause_types) + 1):
            keys.extend([frozenset(pair) for pair in itertools.combinations(root_cause_types, i)])
        return keys

    def add_case(self, case):
        self._validate_and_route_root_causes(case)
        self.cases[case.id] = case
        self._enforce_archive_limit()

        self._update_archive(case)
        self._update_best_case(case)

        root_causes_key = self._normalize_root_causes(case.case_info.get("root_causes"))
        if root_causes_key in self.islands:
            self.islands[root_causes_key].add(case.id)
        else:
            self.islands[root_causes_key] = set([case.id])

        self.save_to_memory(case)
        logger.debug(f"Added case {case.id} to island {root_causes_key}")

        try:
            self._maybe_finetune()
        except Exception as e:
            print(f"Online finetune failed: {e}")
        return case.id

    def save_to_memory(self, new_case=None):
        if new_case is not None:
            try:
                with open(self.db_path, "r") as f:
                    data_to_save = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data_to_save = {}

            data_to_save[new_case.id] = new_case.case_info

            with open(self.db_path, "w") as f:
                json.dump(data_to_save, f)
        else:
            # full save
            with open(self.db_path, "w") as f:
                data_to_save = {}
                for cid, case in self.cases.items():
                    data_to_save[cid] = case.case_info
                json.dump(data_to_save, f)

    def save_case(self, query_info, fix_action, root_causes, old_time, new_time, label, tuning_attempts=None):
        """
        Save a case to memory
        
        Args:
            query_info: Query information
            fix_action: Fix action applied
            root_causes: Root causes (list or single value)
            old_time: Original execution time
            new_time: New execution time after fix
            label: "positive" or "negative"
            tuning_attempts: Dict[str, int] mapping component (root_cause_type) to number of attempts n_{i,b}
                           If None, will be estimated from root_causes
        """
        approve_time = old_time - new_time

        # If tuning_attempts not provided, estimate from root_causes
        if tuning_attempts is None:
            tuning_attempts = {}
            # If positive case, assume at least 1 attempt for each root cause component
            if label == "positive":
                root_causes_list = root_causes if isinstance(root_causes, list) else [root_causes]
                for rc in root_causes_list:
                    if rc in self.root_cause_types:
                        tuning_attempts[rc] = tuning_attempts.get(rc, 0) + 1
            # For negative cases, we can't reliably estimate, so leave as empty dict

        case_info = {
            "query_info": query_info.__dict__,
            "fix_action": fix_action,
            "root_causes": root_causes,
            "old_time": old_time,
            "new_time": new_time,
            "label": label,
            "approve_time": approve_time,
            "tuning_attempts": tuning_attempts,
        }

        case = MemoryCase(
            id=str(len(self.cases) + 1),
            case_info=case_info,
            metrics={},
            sql_embedding=None,
            action_embedding=None,
        )
        self.add_case(case)

        return case.id

    def get_case(self, case_id):
        return self.cases.get(case_id)

    def ids_to_cases(self, ids):
        return [self.cases[cid] for cid in ids]

    def load(self):
        if os.path.exists(self.db_path):
            with open(self.db_path, "r") as f:
                data = json.load(f)
                for cid, case_info in data.items():
                    case = self._build_memory_case_from_info(cid, case_info)
                    self.cases[cid] = case

                    root_causes = case.case_info.get("root_causes")
                    if root_causes:
                        island_key = self._normalize_root_causes(root_causes)
                        self.islands[island_key].add(cid)

                positive_cases = [c for c in self.cases.values() if c.case_info.get("label") == "positive"]
                positive_cases.sort(key=lambda c: c.metrics.get("approve_time", 0.0), reverse=True)
                self.archive = set([c.id for c in positive_cases[: self.archive_size]])
            self._last_finetune_count = len(self.cases)
            # self._last_finetune_count = 0
            # self._maybe_finetune()
        else:
            self._last_finetune_count = 0

    def _build_memory_case_from_info(self, case_id, case_info):
        raw_info = case_info["query_info"]

        if "is_rewrite" not in raw_info:
            raw_info["is_rewrite"] = False
        query_info = QueryInfo(**raw_info)
        sql_embedding = self.build_sql_embedding(query_info)
        action_embedding = self.build_action_embedding(str(case_info["fix_action"]))

        approve_time = case_info.get("approve_time", 0.0)
        new_time = case_info.get("new_time", 0.0)
        metrics = {"performance": new_time, "approve_time": approve_time}

        return MemoryCase(
            id=case_id,
            case_info=case_info,
            metrics=metrics,
            sql_embedding=sql_embedding,
            action_embedding=action_embedding,
        )

    def _feature_coords_to_key(self, coords):
        return "-".join(str(c) for c in coords)

    def _is_better(self, case1, case2):
        return case1.metrics.get("approve_time", 0.0) > case2.metrics.get("approve_time", 0.0)

    # select the best case by approve_time
    def best_by_approve(self, cases):
        if not cases:
            return None
        return max(
            cases,
            key=lambda c: c.case_info.get("approve_time") or c.metrics.get("approve_time", 0.0),
        )

    def _update_archive(self, case):
        # only positive cases are allowed to enter archive
        if case.case_info.get("label") != "positive":
            return

        if len(self.archive) < self.archive_size:
            self.archive.add(case.id)
            return
        archive_cases = [self.cases[aid] for aid in self.archive]
        worst_case = min(archive_cases, key=lambda c: c.metrics.get("approve_time", 0.0))
        if self._is_better(case, worst_case):
            self.archive.remove(worst_case.id)
            self.archive.add(case.id)

    def _update_best_case(self, case):
        # only positive cases are allowed to become best_case
        if case.case_info.get("label") != "positive":
            return

        if self.best_case_id is None or self._is_better(case, self.cases[self.best_case_id]):
            self.best_case_id = case.id

    def _enforce_archive_limit(self):
        if len(self.archive) > self.archive_size:
            # sort by approve_time
            archive_cases = [self.cases[case_id] for case_id in self.archive if case_id in self.cases]
            sorted_archive_cases = sorted(archive_cases, key=lambda c: c.metrics.get("approve_time"))

            # remove the worst cases
            to_remove_count = len(self.archive) - self.archive_size
            to_remove_cases = sorted_archive_cases[:to_remove_count]

            for case in to_remove_cases:
                if case.id in self.archive:
                    self.archive.remove(case.id)

    def retrieve_cases(self, query_info, root_causes, pos_n=2, neg_n=2, mode="exploit"):
        """
        Unified case retrieval interface, selects static or dynamic retrieval based on configured retrieval_mode
        """
        if self.retrieval_mode == "static":
            return self.retrieve_static_cases(query_info, root_causes, pos_n, neg_n, mode)
        else:
            return self.retrieve_dynamic_cases(query_info, root_causes, pos_n, neg_n, mode)

    def retrieve_dynamic_cases(self, query_info, root_causes, pos_n=2, neg_n=2, mode="exploit"):
        # Get all candidate cases
        all_cases = list(self.cases.values())
        if not all_cases:
            return {"mode": mode, "positive": [], "negative": []}

        # Filter candidate cases based on mode
        if mode == "exploit":
            # Exploit mode: select cases with same root cause
            candidate_cases = []
            for case in all_cases:
                case_root_causes = case.case_info.get("root_causes")
                if any(rc in case_root_causes for rc in root_causes):
                    candidate_cases.append(case.case_info)
        elif mode == "explore":
            # Explore mode: select cases with different root causes
            candidate_cases = []
            for case in all_cases:
                case_root_causes = case.case_info.get("root_causes")
                if not any(rc in case_root_causes for rc in root_causes):
                    candidate_cases.append(case.case_info)

        # If no candidate cases, return empty result
        if not candidate_cases:
            return {"mode": mode, "positive": [], "negative": []}

        # Use Soft Q Learning retriever to select cases
        selected_cases = self.retriever.select_cases(query_info.__dict__, candidate_cases, root_causes, top_k=pos_n + neg_n)

        # Classify by label
        positive_cases = []
        negative_cases = []

        for case_info, probability in selected_cases:
            if case_info.get("label") == "positive":
                positive_cases.append(case_info)
            else:
                negative_cases.append(case_info)

        # Limit quantity
        positive_cases = positive_cases[:pos_n]
        negative_cases = negative_cases[:neg_n]

        return {
            "mode": mode,
            "positive": positive_cases,
            "negative": negative_cases,
        }

    def retrieve_static_cases(self, query_info, root_causes, pos_n=2, neg_n=2, mode="exploit"):
        """
        Use traditional islands to construct sample pool and output by label:
        - exploitation pool: island corresponding to target root cause
        - exploration pool: all other islands merged
        Positive/Negative division based on case_info.label (fallback to approve_time>0 as positive if missing).
        Positive return rule: include at least 1 with same SQL and 1 with different SQL with max approve_time each, rest filled in descending order of improvement up to pos_n.
        Negative return rule: randomly sample at most neg_n from negative pool.
        Returns: {"mode": str, "positive": [case_info], "negative": [case_info]}
        """
        query_text = query_info.query.lower().strip()
        target_root = self._normalize_root_causes(root_causes)

        target_island_ids = list(self.islands.get(target_root, set()))
        other_island_ids = []
        for island_key, ids in self.islands.items():
            if island_key != target_root:
                other_island_ids.extend(list(ids))

        # Exploitation pool
        exploit_pool = self.ids_to_cases(target_island_ids)
        explore_pool = self.ids_to_cases(other_island_ids)

        # ε-greedy mode
        chosen_pool = explore_pool if mode == "explore" else exploit_pool

        # In exploit pool, prioritize same SQL cases
        def split_by_sql(cases):
            same_sql_list = []
            diff_sql_list = []
            for c in cases:
                stored_query = c.case_info.get("query_info").get("query")
                if stored_query.lower().strip() == query_text:
                    same_sql_list.append(c)
                else:
                    diff_sql_list.append(c)
            return same_sql_list, diff_sql_list

        # Split by label/approve_time
        same_sql_pool, diff_sql_pool = split_by_sql(chosen_pool)

        # Positive pool and negative pool
        pos_same = [c for c in same_sql_pool if c.case_info.get("label") == "positive"]
        pos_diff = [c for c in diff_sql_pool if c.case_info.get("label") == "positive"]
        neg_all = [c for c in chosen_pool if c.case_info.get("label") != "positive"]

        selected_pos = []
        best_same = self.best_by_approve(pos_same)
        if best_same is not None:
            selected_pos.append(best_same)
        best_diff = self.best_by_approve(pos_diff)
        if best_diff is not None:
            selected_pos.append(best_diff)

        # Remaining positive cases are sorted by approve_time
        remaining_pos = [c for c in (pos_same + pos_diff) if c not in selected_pos]
        remaining_pos_sorted = sorted(
            remaining_pos,
            key=lambda c: c.case_info.get("approve_time") or c.metrics.get("approve_time", 0.0),
            reverse=True,
        )
        selected_pos.extend(remaining_pos_sorted)
        selected_pos = selected_pos[: max(1, pos_n)]

        # Negative cases are sampled randomly
        if len(neg_all) > neg_n:
            selected_neg = random.sample(neg_all, neg_n)
        else:
            selected_neg = neg_all

        # Export case_info
        pos_infos = [c.case_info for c in selected_pos]
        neg_infos = [c.case_info for c in selected_neg]

        return {"mode": mode, "positive": pos_infos, "negative": neg_infos}

    def retrieve_embedding(self, query_info, top_n=5, metric="cosine", case_ids=None):
        query_emb = np.array(self.build_sql_embedding(query_info))
        if norm(query_emb) == 0:
            return []

        # the embedding similarity
        case_sims = []
        if case_ids is not None:
            candidates = [self.get_case(cid) for cid in case_ids]
        else:
            candidates = list(self.cases.values())
        for case in candidates:
            if case.sql_embedding is None:
                continue
            case_emb = np.array(case.sql_embedding)
            if metric == "cosine":
                sim = np.dot(query_emb, case_emb) / (norm(query_emb) * norm(case_emb) + 1e-8)
            elif metric == "euclidean":
                sim = -norm(query_emb - case_emb)
            else:
                raise ValueError("Unknown metric")
            case_sims.append((sim, case))

        # sort and return top-N
        case_sims.sort(reverse=True, key=lambda x: x[0])
        return [c for _, c in case_sims[:top_n]]

    def build_sql_embedding(self, query_info):
        # use the RCRankPredictor
        if not hasattr(self, "_rcrank_predictor") or self._rcrank_predictor is None:
            from agent.plan.online_predict import RCRankPredictor

            if self._planner_config is None:
                self._planner_config = {
                    "device": "cpu",
                    "model_path": "/root/DREAM/src/agent/plan/model_res/GateComDiffPretrainModel slow_sql_data_gen eta0.07/best_model.pt",
                    "train_data_path": "/root/DREAM/src/agent/plan/slow_sql_data_gen.csv",
                    "opt_threshold": 0.5,
                    "num_classes": 4
                }
            self._rcrank_predictor = RCRankPredictor(self._planner_config)
        predictor = self._rcrank_predictor

        import pandas as pd

        from dream.agent.plan.RCRank.model.modules.QueryFormer.utils import Encoding
        from dream.agent.plan.RCRank.utils.plan_encoding import PlanEncoder

        encoding = Encoding(None, {"NA": 0})

        query = query_info.query
        query_plan = query_info.plan_json

        df = pd.DataFrame([{"plan_json": (json.dumps({"Plan": query_plan}) if isinstance(query_plan, dict) else query_plan)}])
        plan_encoder = PlanEncoder(df=df, encoding=encoding)
        encoded_df = plan_encoder.df
        plan_tensor = encoded_df["json_plan_tensor"].iloc[0]

        sql = predictor.tokenizer(query, return_tensors="pt", padding=True, truncation=True, max_length=512).to(predictor.device)

        plan = {
            "x": plan_tensor["x"].to(predictor.device).to(torch.float32),
            "attn_bias": plan_tensor["attn_bias"].to(predictor.device),
            "rel_pos": plan_tensor["rel_pos"].to(predictor.device),
            "heights": plan_tensor["heights"].to(predictor.device),
        }

        timeseries = torch.tensor(query_info.external_metrics)
        if len(timeseries.shape) == 2:
            timeseries = timeseries.unsqueeze(0)
        timeseries = (timeseries - predictor.timeseries_train_mean.unsqueeze(1)) / (predictor.timeseries_train_std.unsqueeze(1) + 1e-6)
        timeseries = timeseries.to(predictor.device)

        log = torch.tensor(query_info.internal_metrics)
        if len(log.shape) == 1:
            log = log.unsqueeze(0)
        log = (log - predictor.logs_train_mean) / (predictor.logs_train_std + 1e-6)
        log = log.to(predictor.device)

        # get the embedding
        emb = predictor.model.get_embedding(sql, plan, timeseries, log)
        return emb.squeeze(0).cpu().numpy().tolist()

    def build_action_embedding(self, action):
        from transformers import BertModel, BertTokenizer

        if not hasattr(self, "_bert_tokenizer"):
            bert_path = self.bert_model_path
            self._bert_tokenizer = BertTokenizer.from_pretrained(bert_path)
            self._bert_model = BertModel.from_pretrained(bert_path)
            self._bert_model.eval()
        tokenizer = self._bert_tokenizer
        model = self._bert_model

        inputs = tokenizer(action, return_tensors="pt", padding=True, truncation=True, max_length=128)
        with torch.no_grad():
            outputs = model(**inputs)
            cls_emb = outputs.last_hidden_state[:, 0, :]  # [CLS] vector, 768 dimensions
            # Directly truncate to 64 dimensions (take first 64 dimensions)
            truncated_emb = cls_emb[:, :64]  # 768 -> 64
        return truncated_emb.squeeze(0).cpu().numpy().tolist()

    def _normalize_root_causes(self, root_causes):
        if root_causes is None:
            return frozenset()
        if isinstance(root_causes, str):
            return frozenset([root_causes])
        try:
            return frozenset(root_causes)
        except Exception:
            return frozenset([str(root_causes)])

    def _infer_root_causes_from_action(self, fix_action, query_info):
        fix_action = str(fix_action).lower()
        query_text = query_info.query.lower().strip()
        inferred: Set[str] = set()
        if "index" in fix_action:
            inferred.add("missing indexes")
        if ("set" in fix_action) and ("=" in fix_action):
            inferred.add("inappropriate query knobs")
        if ("/*+" in query_text) or ("*/" in query_text):
            inferred.add("suboptimal plan optimizer")
        if query_info.is_rewrite:
            inferred.add("poorly written queries")
        return frozenset(inferred) if inferred else frozenset()

    def _validate_and_route_root_causes(self, case):
        # validate and route the root causes
        declared = self._normalize_root_causes(case.case_info.get("root_causes"))
        raw_info = case.case_info.get("query_info")
        query_info = QueryInfo(**raw_info)
        inferred = self._infer_root_causes_from_action(case.case_info.get("fix_action"), query_info)

        if inferred and (inferred != declared):
            case.case_info["root_causes"] = list(inferred)

        key = self._normalize_root_causes(case.case_info.get("root_causes"))
        if key not in self.islands:
            self.islands[key] = set()

    def explore_root_from_archive(self, query_info, exclude_roots):
        """
        retrieve the most similar SQL root cause from archive
        return the most similar SQL root cause, if not found, return None
        """
        if not self.archive:
            return None

        exclude_roots = exclude_roots

        archive_cases = [self.cases[aid] for aid in self.archive]
        if not archive_cases:
            return None

        similar_cases = self.retrieve_embedding(query_info, top_n=len(archive_cases), case_ids=list(self.archive))

        for case in similar_cases:
            case_root_str = str(self._normalize_root_causes(case.case_info.get("root_causes")))
            if case_root_str not in exclude_roots:
                return case.case_info.get("root_causes")

        return None

    def save_experience(self, query_info, case_info, root_causes, reward, next_query_info=None, next_root_causes=None, done=False):
        """
        Save single experience to file (JSONL format, append mode)
        
        Experience tuple: (s_t, o_t, B_t, r_t, s_{t+1}, B_{t+1}, done)
        
        Before saving the new experience, update the previous experience with the same query_id
        by setting its next_query_info and next_root_causes to the current query_info and root_causes.
        
        Args:
            query_info: s_t - current state (query_info)
            case_info: o_t - retrieved observation from memory
            root_causes: B_t - current bottleneck components
            reward: r_t - reward
            next_query_info: s_{t+1} - next state (query_info), None if terminal
            next_root_causes: B_{t+1} - next bottleneck components, None if terminal
            done: Whether the episode is terminated
        """

        if not self.samples_save_path:
            return

        os.makedirs(os.path.dirname(self.samples_save_path), exist_ok=True)

        query_id = query_info.query_id

        # If there's a pending experience with the same query_id, update it first
        if query_id in self.pending_experiences:
            pending_exp = self.pending_experiences[query_id]
            # Update the pending experience's next_query_info and next_root_causes
            pending_exp["exp_dict"]["next_query_info"] = query_info.__dict__ if hasattr(query_info, '__dict__') else query_info
            pending_exp["exp_dict"]["next_root_causes"] = root_causes if isinstance(root_causes, list) else [root_causes] if root_causes else []
            pending_exp["exp_dict"]["done"] = False
            
            # Update the line in the file
            self._update_experience_in_file(pending_exp["line_number"], pending_exp["exp_dict"])
            
            # Remove from pending experiences
            del self.pending_experiences[query_id]

        # Prepare current experience dict
        exp_dict = {
            "query_info": query_info.__dict__ if hasattr(query_info, '__dict__') else query_info,
            "case_info": case_info,
            "root_causes": root_causes if isinstance(root_causes, list) else [root_causes] if root_causes else [],
            "reward": reward,
            "next_query_info": next_query_info.__dict__ if next_query_info and hasattr(next_query_info, '__dict__') else next_query_info,
            "next_root_causes": next_root_causes if isinstance(next_root_causes, list) else [next_root_causes] if next_root_causes else [],
            "done": done,
        }

        # Get current line number (count lines in file)
        line_number = self._get_file_line_count()

        # Save current experience to file
        with open(self.samples_save_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(exp_dict, ensure_ascii=False) + "\n")

        # If this experience doesn't have next_query_info and is not done, track it as pending
        if query_id and (exp_dict["next_query_info"] is None) and not done:
            self.pending_experiences[query_id] = {
                "line_number": line_number,
                "exp_dict": exp_dict.copy()
            }

    def _get_file_line_count(self):
        """Get the number of lines in the samples file"""
        if not os.path.exists(self.samples_save_path):
            return 0
        with open(self.samples_save_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)

    def _load_pending_experiences(self):
        """Load pending experiences (without next_query_info) from file"""
        if not self.samples_save_path or not os.path.exists(self.samples_save_path):
            return
        
        self.pending_experiences = {}
        with open(self.samples_save_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    exp_dict = json.loads(line)
                    query_info = exp_dict.get("query_info", {})
                    query_id = query_info.get("query_id") if isinstance(query_info, dict) else None
                    
                    # If this experience doesn't have next_query_info and is not done, track it as pending
                    if query_id and exp_dict.get("next_query_info") is None and not exp_dict.get("done", False):
                        self.pending_experiences[query_id] = {
                            "line_number": line_num,
                            "exp_dict": exp_dict
                        }
                except json.JSONDecodeError:
                    continue

    def _update_experience_in_file(self, line_number, updated_exp_dict):
        """
        Update a specific line in the JSONL file with updated experience dict
        
        Args:
            line_number: 0-based line number to update
            updated_exp_dict: Updated experience dictionary
        """
        if not os.path.exists(self.samples_save_path):
            return

        # Read all lines
        with open(self.samples_save_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Update the specific line
        if 0 <= line_number < len(lines):
            lines[line_number] = json.dumps(updated_exp_dict, ensure_ascii=False) + "\n"
            
            # Write back to file
            with open(self.samples_save_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
