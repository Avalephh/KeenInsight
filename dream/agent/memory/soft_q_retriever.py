import os
import random
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict

import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class Experience:
    query_info: Dict[str, Any]
    root_causes: list
    case_info: Dict[str, Any]
    reward: float
    next_query_info: Dict[str, Any]
    next_root_causes: list
    done: bool = False


class SoftQRetriever:
    """Case retriever based on soft Q-learning"""

    def __init__(self, memory_manager=None):
        self.device = "cpu"

        self.embedding_dim = 133
        self.hidden_dim = 512
        self.output_dim = 1
        self.temperature = 1.0

        # Soft Q-learning parameters
        self.gamma = 0.99  # Discount factor γ
        self.alpha = 1.0  # Temperature parameter α for entropy regularization

        # Experience pool parameters
        self.batch_size = 2
        self.learning_rate = 1e-4

        # Store memory_manager reference
        self.memory_manager = memory_manager

        # Initialize network
        self._init_networks()

        # Experience pool (unlimited size, as experiences are persisted to file)
        self.experience_pool = deque()

        # Track pending experiences (experiences without next_query_info) by query_id
        # Format: {query_id: Experience}
        self.pending_experiences = {}

        # Optimizer
        self.optimizer = torch.optim.Adam(self.network.parameters(), lr=self.learning_rate)

        # Statistics
        self.total_experiences = 0
        self.update_count = 0

    def _init_networks(self):
        # Main network: two-layer MLP (outputs Q-value, not probability)
        # According to paper: Q^π(s_t, M_t, o_t) is a Q-value, not a probability
        # The softmax over Q-values is applied in select_cases, not in the network
        self.network = nn.Sequential(
            nn.Linear(self.embedding_dim, self.hidden_dim),  # query_emb(32) + case_emb(64+32) + root_causes(4) + approve_time(1) = 133
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(self.hidden_dim, self.hidden_dim // 2),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(self.hidden_dim // 2, self.output_dim),
            # No activation: output raw Q-value (can be any real number)
        ).to(self.device)

        # Target network (for stable training)
        self.target_network = nn.Sequential(
            nn.Linear(self.embedding_dim, self.hidden_dim),  # 133-dimensional input
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(self.hidden_dim, self.hidden_dim // 2),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(self.hidden_dim // 2, self.output_dim),
            # No activation: output raw Q-value
        ).to(self.device)

        self.target_network.load_state_dict(self.network.state_dict())

    def _build_query_embedding(self, query_info) -> torch.Tensor:
        from utils.types import QueryInfo

        query_obj = QueryInfo(**query_info)
        embedding_list = self.memory_manager.build_sql_embedding(query_obj)

        embedding = torch.tensor(embedding_list, dtype=torch.float32)

        return embedding.to(self.device)

    def _build_case_embedding(self, case_info) -> torch.Tensor:
        from utils.types import QueryInfo

        case_query_info = case_info.get("query_info")
        case_query_obj = QueryInfo(**case_query_info)
        query_embedding = self.memory_manager.build_sql_embedding(case_query_obj)

        fix_action = case_info.get("fix_action", "")
        action_embedding = self.memory_manager.build_action_embedding(fix_action)

        combined_embedding = query_embedding + action_embedding

        embedding = torch.tensor(combined_embedding, dtype=torch.float32)

        return embedding.to(self.device)

    def _encode_root_causes(self, root_causes) -> torch.Tensor:
        root_cause_types = [
            "missing indexes",
            "inappropriate query knobs",
            "suboptimal plan optimizer",
            "poorly written queries",
        ]

        root_cause_vector = torch.zeros(4)
        for i, root_cause_type in enumerate(root_cause_types):
            if root_cause_type in root_causes:
                root_cause_vector[i] = 1.0

        return root_cause_vector.to(self.device)

    def _compute_features(self, query_info, case_info, root_causes):
        query_embedding = self._build_query_embedding(query_info)
        case_embedding = self._build_case_embedding(case_info)

        root_cause_vector = self._encode_root_causes(root_causes)

        approve_time = case_info.get("approve_time", 0.0)
        approve_time_tensor = torch.tensor([approve_time], dtype=torch.float32).to(self.device)

        features = torch.cat([query_embedding, case_embedding, root_cause_vector, approve_time_tensor])

        return features

    def predict_case_value(self, query_info, case_info, root_causes):
        """
        Predict Q-value for a single case
        
        Returns:
            Q-value (raw output from network, can be any real number)
        """
        with torch.no_grad():
            features = self._compute_features(query_info, case_info, root_causes)
            features = features.unsqueeze(0)

            q_value = self.network(features)
            value = q_value.item()

            return value

    def select_cases(self, query_info, candidate_cases, root_causes, top_k=4):
        """
        Select TopK cases using softmax over Q-values according to paper:
        μ*(o | s, M) = exp(Q*(s, M, o)/α) / Σ_{o' in M} exp(Q*(s, M, o')/α)
        
        Then select TopK cases based on softmax probabilities.
        """
        if not candidate_cases:
            return []

        # Calculate Q-value for each case
        case_q_values = []
        for case_info in candidate_cases:
            q_value = self.predict_case_value(query_info, case_info, root_causes)
            case_q_values.append((case_info, q_value))

        # Convert to tensors for softmax computation
        cases_list = [case_info for case_info, _ in case_q_values]
        q_values_list = [q_val for _, q_val in case_q_values]
        q_values_tensor = torch.tensor(q_values_list, dtype=torch.float32, device=self.device)

        # Apply softmax over Q-values with temperature α
        # μ*(o | s, M) = exp(Q*(s, M, o)/α) / Σ_{o' in M} exp(Q*(s, M, o')/α)
        q_values_scaled = q_values_tensor / self.alpha
        softmax_probs = F.softmax(q_values_scaled, dim=0)

        # Get probabilities as list
        probabilities = softmax_probs.cpu().tolist()

        # Combine cases with their softmax probabilities
        case_probs = list(zip(cases_list, probabilities))

        # Sort by probability (descending)
        case_probs.sort(key=lambda x: x[1], reverse=True)

        # Select TopK
        selected_cases = case_probs[:top_k]

        # Return cases and their softmax probabilities
        result = []
        for case_info, probability in selected_cases:
            result.append((case_info, probability))

        return result

    def add_experience(self, query_info, case_info, root_causes, reward, next_query_info, next_root_causes, done=False):
        """
        Add experience tuple (s_t, o_t, B_t, r_t, s_{t+1}, B_{t+1}, done)
        
        Before adding the new experience, update the previous experience with the same query_id
        by setting its next_query_info and next_root_causes to the current query_info and root_causes.
        
        Only complete experiences (with next_query_info) are added to experience_pool.
        Incomplete experiences (without next_query_info) are stored in pending_experiences.
        
        Args:
            query_info: s_t - current state (query_info, can be dict or QueryInfo object)
            case_info: o_t - retrieved observation from memory
            root_causes: B_t - current bottleneck components
            reward: r_t - reward
            next_query_info: s_{t+1} - next state (query_info), None if terminal
            next_root_causes: B_{t+1} - next bottleneck components, None if terminal
            done: Whether the episode is terminated
        """
        query_id = query_info.query_id
        
        # If there's a pending experience with the same query_id, complete it and add to pool
        if query_id in self.pending_experiences:
            pending_exp = self.pending_experiences[query_id]
            # Update the pending experience's next_query_info and next_root_causes
            pending_exp.next_query_info = query_info.__dict__ if hasattr(query_info, '__dict__') else query_info
            pending_exp.next_root_causes = root_causes if isinstance(root_causes, list) else [root_causes] if root_causes else []
            pending_exp.done = False  # Not terminal since we have next state
            
            # Now it's complete, add to experience_pool
            self.experience_pool.append(pending_exp)
            self.total_experiences += 1
            
            # Remove from pending experiences
            del self.pending_experiences[query_id]
        
        # Create current experience
        experience = Experience(
            query_info=query_info.__dict__ if hasattr(query_info, '__dict__') else query_info,
            case_info=case_info,
            root_causes=root_causes if isinstance(root_causes, list) else [root_causes] if root_causes else [],
            reward=reward,
            next_query_info=next_query_info.__dict__ if next_query_info and hasattr(next_query_info, '__dict__') else next_query_info,
            next_root_causes=next_root_causes if isinstance(next_root_causes, list) else [next_root_causes] if next_root_causes else [],
            done=done
        )

        # Only add complete experiences to experience_pool
        if experience.next_query_info is not None or done:
            # Complete experience: has next_query_info or is terminal
            self.experience_pool.append(experience)
            self.total_experiences += 1
        else:
            self.pending_experiences[query_id] = experience

    def _get_next_memory_cases(self, next_query_info, next_root_causes):
        if self.memory_manager is None or next_query_info is None:
            return []
        
        target_case_id = None
        for case_id, case in self.memory_manager.cases.items():
            case_query_info = case.case_info.get("query_info")
            if case_query_info == next_query_info:
                target_case_id = case_id
                break
        
        if target_case_id is not None:
            cases_list = [(int(case_id), case.case_info) for case_id, case in self.memory_manager.cases.items()]
            cases_list.sort(key=lambda x: x[0])
            return [case_info for _, case_info in cases_list[:int(target_case_id)]]
        else:
            return [case.case_info for case in self.memory_manager.cases.values()]

    def _compute_soft_bellman_target(self, next_query_info, next_root_causes, next_memory_cases, done):
        """
        Compute soft Bellman target: y_t = r_t + γ * α * log(Σ_{o' in M_{t+1}} exp(Q_θ(s_{t+1}, M_{t+1}, o')/α))
        
        Args:
            next_query_info: s_{t+1} - next state
            next_root_causes: B_{t+1} - next bottleneck components
            next_memory_cases: List of case_info in M_{t+1} (next memory)
            done: Whether episode is terminated
        
        Returns:
            TD target value
        """
        if done or len(next_memory_cases) == 0:
            return torch.tensor(0.0, device=self.device)
        
        # Compute Q-values for all observations in next memory
        q_values_next = []
        for case_info in next_memory_cases:
            with torch.no_grad():
                features_next = self._compute_features(next_query_info, case_info, next_root_causes)
                features_next = features_next.unsqueeze(0)
                q_value = self.target_network(features_next).squeeze()
                q_values_next.append(q_value)
        
        if len(q_values_next) == 0:
            return torch.tensor(0.0, device=self.device)
        
        # Stack Q-values: [num_cases]
        q_values_tensor = torch.stack(q_values_next)  # [num_cases]
        
        # Compute soft Bellman backup: α * log(Σ exp(Q/α))
        # For numerical stability, use log-sum-exp trick
        q_values_scaled = q_values_tensor / self.alpha
        q_max = torch.max(q_values_scaled)
        log_sum_exp = q_max + torch.log(torch.sum(torch.exp(q_values_scaled - q_max)) + 1e-12)
        soft_value = self.alpha * log_sum_exp
        
        return soft_value

    def update_network(self, next_memory_cases_fn=None):
        if len(self.experience_pool) < self.batch_size:
            return

        # Randomly sample experiences
        batch_experiences = random.sample(self.experience_pool, self.batch_size)

        # Prepare training data
        features_list = []
        td_targets = []

        for experience in batch_experiences:
            # Compute features for (s_t, o_t, B_t)
            features = self._compute_features(
                experience.query_info,
                experience.case_info,
                experience.root_causes,
            )
            features_list.append(features)
            
            # Compute TD target: y_t = r_t + γ * α * log(Σ_{o' in M_{t+1}} exp(Q_θ(s_{t+1}, M_{t+1}, o')/α))
            if experience.done:
                # Terminal state: y_t = r_t
                td_target = torch.tensor(experience.reward, dtype=torch.float32, device=self.device)
            else:
                # Get next memory cases M_{t+1}
                next_memory_cases = self._get_next_memory_cases(experience.next_query_info, experience.next_root_causes)
                
                # Compute soft Bellman target
                soft_value = self._compute_soft_bellman_target(
                    experience.next_query_info,
                    experience.next_root_causes,
                    next_memory_cases,
                    experience.done
                )
                
                # TD target: y_t = r_t + γ * soft_value
                td_target = torch.tensor(experience.reward, dtype=torch.float32, device=self.device) + self.gamma * soft_value
            
            td_targets.append(td_target)

        # Convert to tensors
        features_tensor = torch.stack(features_list)
        td_targets_tensor = torch.stack(td_targets)

        # Forward pass: Q_θ(s_t, M_t, o_t)
        q_predictions = self.network(features_tensor).squeeze()

        # Compute TD loss: L_TD(θ) = E[(Q_θ(s_t, M_t, o_t) - y_t)^2]
        loss = F.mse_loss(q_predictions, td_targets_tensor)

        # Backward pass
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        self.update_count += 1

        # Periodically update target network
        if self.update_count % 10 == 0:
            self.target_network.load_state_dict(self.network.state_dict())

    def save_model(self, save_path):
        os.makedirs(os.path.dirname(save_path), exist_ok=True)

        torch.save(self.network.state_dict(), save_path)
        print(f"SoftQRetriever model saved to: {save_path}")

    def load_model(self, load_path):
        state_dict = torch.load(load_path, map_location=self.device)

        self.network.load_state_dict(state_dict)
        self.target_network.load_state_dict(state_dict)

        print(f"SoftQRetriever model loaded from: {load_path}")
