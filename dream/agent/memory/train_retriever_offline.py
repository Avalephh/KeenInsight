"""
Offline training script for Soft Q Retriever
Train the retriever model based on sample files
"""

import argparse
import json
import os
import sys
from pathlib import Path

import torch
import torch.nn.functional as F

from dream.agent.memory.memory_manager import MemoryManager
from dream.agent.plan.online_predict import RCRankPredictor


def load_samples(samples_path):
    """
    Load samples from JSONL file (Experience format)
    Expected format: (s_t, o_t, B_t, r_t, s_{t+1}, B_{t+1}, done)
    """
    samples = []
    if not os.path.exists(samples_path):
        print(f"Error: Sample file does not exist: {samples_path}")
        return samples

    line_count = 0
    with open(samples_path, "r", encoding="utf-8") as f:
        for line in f:
            line_count += 1
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
                if sample.get("next_query_info") is None or sample.get("next_root_causes") is None:
                    sample["done"] = True
                samples.append(sample)
            except json.JSONDecodeError as e:
                print(f"Warning: Failed to parse line {line_count}: {e}")
                continue

    print(f"Successfully loaded {len(samples)} valid samples (read {line_count} lines total)")
    return samples


def train_retriever_from_samples(
    samples_path,
    model_save_path,
    memory_manager_config,
    epochs=10,
    batch_size=32,
    learning_rate=1e-4,
):
    """
    Train retriever from sample file

    Args:
        samples_path: Path to sample file (JSONL format)
        model_save_path: Path to save the trained model
        memory_manager_config: MemoryManager configuration dictionary
        epochs: Number of training epochs
        batch_size: Batch size
        learning_rate: Learning rate
    """
    # Load samples
    samples = load_samples(samples_path)
    if len(samples) == 0:
        print("Error: No available sample data")
        return

    # Initialize MemoryManager and Retriever
    planner_config = memory_manager_config.get("PLANNER_CONFIG", {})
    if not planner_config:
        planner_config = {"device": "cpu"}
    predictor = RCRankPredictor(planner_config)
    memory_manager = MemoryManager(memory_manager_config, predictor=predictor)
    retriever = memory_manager.retriever

    # Update training parameters
    retriever.batch_size = batch_size
    retriever.learning_rate = learning_rate
    retriever.optimizer = torch.optim.Adam(retriever.network.parameters(), lr=learning_rate)
    
    # Set soft Q-learning parameters (can be made configurable)
    retriever.gamma = 0.99  # Discount factor γ
    retriever.alpha = 1.0  # Temperature parameter α

    # Training loop
    print(f"Starting training, {epochs} epochs...")
    for epoch in range(epochs):
        print(f"Epoch {epoch + 1}/{epochs}")
        epoch_loss = 0.0
        batch_count = 0

        # Shuffle data
        import random

        random.shuffle(samples)

        # Batch training
        valid_batches = 0
        for i in range(0, len(samples), batch_size):
            print(f"Processing batch {i // batch_size + 1}/{len(samples) // batch_size}")
            batch_samples = samples[i : i + batch_size]

            if len(batch_samples) < batch_size:
                continue  # Skip incomplete batch (last incomplete batch)

            # Prepare training data
            features_list = []
            td_targets = []

            for sample in batch_samples:
                query_info = sample.get("query_info", {})
                case_info = sample.get("case_info", {})
                root_causes = sample.get("root_causes", [])
                reward = sample.get("reward", 0.0)
                next_query_info = sample.get("next_query_info")
                next_root_causes = sample.get("next_root_causes", [])
                done = sample.get("done", False)

                # Compute features for (s_t, o_t, B_t)
                features = retriever._compute_features(query_info, case_info, root_causes)
                features_list.append(features)
                
                # Compute TD target: y_t = r_t + γ * α * log(Σ_{o' in M_{t+1}} exp(Q_θ(s_{t+1}, M_{t+1}, o')/α))
                if done or next_query_info is None:
                    # Terminal state: y_t = r_t
                    td_target = torch.tensor(reward, dtype=torch.float32, device=retriever.device)
                else:
                    # Get next memory cases M_{t+1}
                    next_memory_cases = retriever._get_next_memory_cases(next_query_info, next_root_causes)
                    
                    # Compute soft Bellman target
                    soft_value = retriever._compute_soft_bellman_target(
                        next_query_info,
                        next_root_causes,
                        next_memory_cases,
                        done
                    )
                    
                    # TD target: y_t = r_t + γ * soft_value
                    td_target = torch.tensor(reward, dtype=torch.float32, device=retriever.device) + retriever.gamma * soft_value
                
                td_targets.append(td_target)

            # Ensure all features have consistent dimensions
            if len(features_list) > 0:
                feature_dim = features_list[0].shape[0]
                # Filter out features with inconsistent dimensions
                valid_features = []
                valid_targets = []
                for feat, target in zip(features_list, td_targets):
                    if feat.shape[0] == feature_dim:
                        valid_features.append(feat)
                        valid_targets.append(target)

                features_list = valid_features
                td_targets = valid_targets

            if len(features_list) == 0:
                continue

            # Convert to tensors
            features_tensor = torch.stack(features_list)
            td_targets_tensor = torch.stack(td_targets)

            # Forward pass: Q_θ(s_t, M_t, o_t)
            q_predictions = retriever.network(features_tensor).squeeze()

            # Compute TD loss: L_TD(θ) = E[(Q_θ(s_t, M_t, o_t) - y_t)^2]
            loss = F.mse_loss(q_predictions, td_targets_tensor)

            # Backward pass
            retriever.optimizer.zero_grad()
            loss.backward()

            # Gradient clipping (optional, prevents gradient explosion)
            torch.nn.utils.clip_grad_norm_(retriever.network.parameters(), max_norm=1.0)

            retriever.optimizer.step()

            epoch_loss += loss.item()
            batch_count += 1
            valid_batches += 1

        avg_loss = epoch_loss / max(batch_count, 1)
        print(f"Epoch {epoch + 1}/{epochs}, Average TD Loss: {avg_loss:.6f}, Valid Batches: {valid_batches}")

        # # Update target network
        # if (epoch + 1) % 5 == 0:
        #     retriever.target_network.load_state_dict(retriever.network.state_dict())
        #     print(f"Updated target network (Epoch {epoch + 1})")

    # Save model
    retriever.save_model(model_save_path)
    print(f"Training completed! Model saved to: {model_save_path}")
    print(f"Processed {len(samples)} samples over {epochs} epochs")


def main():
    parser = argparse.ArgumentParser(description="Offline training for Soft Q Retriever")
    parser.add_argument(
        "--samples_path",
        type=str,
        required=True,
        help="Path to sample file (JSONL format)",
    )
    parser.add_argument(
        "--model_save_path",
        type=str,
        required=True,
        help="Path to save the trained model",
    )
    parser.add_argument("--config_path", type=str, required=True, help="Path to configuration file")
    parser.add_argument("--epochs", type=int, default=10, help="Number of training epochs")
    parser.add_argument("--batch_size", type=int, default=32, help="Batch size")
    parser.add_argument("--learning_rate", type=float, default=1e-4, help="Learning rate")

    args = parser.parse_args()

    # Load configuration (JSON format)
    with open(args.config_path, "r", encoding="utf-8") as f:
        configs = json.load(f)

    memory_manager_config = configs.get("MEMORY_MANAGER_CONFIG")

    # If PLANNER_CONFIG is not in configs, add default configuration
    if "PLANNER_CONFIG" not in configs:
        configs["PLANNER_CONFIG"] = {"device": "cpu"}

    # Add PLANNER_CONFIG to memory_manager_config if needed
    if "PLANNER_CONFIG" not in memory_manager_config:
        memory_manager_config["PLANNER_CONFIG"] = configs["PLANNER_CONFIG"]

    # Start training
    train_retriever_from_samples(
        samples_path=args.samples_path,
        model_save_path=args.model_save_path,
        memory_manager_config=memory_manager_config,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
    )


# cd /root/DREAM/src/agent/memory
# python train_retriever_offline.py \
#     --samples_path experience/retriever_samples_tpch.jsonl \
#     --model_save_path model/soft_q_retriever_tpch.pt \
#     --config_path ../../../config/tpch_config.json \
#     --epochs 10 \
#     --batch_size 32 \
#     --learning_rate 0.0001

if __name__ == "__main__":
    main()
