# DREAM

Source code for the DREAM framework, proposed in "From Anomalies to Actions: An Anomaly-Aware Approach for Multi-Component Tuning in DBMSs". Please refer to the paper for the experimental details.

## Setup and Run

1) Environment setup

```bash
# Clone repository
git clone https://github.com/HarneyHong/DREAM.git
cd DREAM
git submodule update --init --recursive src/agent/plan/RCRank

# Create conda environment
conda create -n DREAM python=3.10
conda activate DREAM

# Install dependencies
pip install -r requirements.txt
```

2) Configure APIs

```bash
cd config
cp tpch_config.json.example tpch_config.json
cp tpcds_config.json.example tpcds_config.json
cp job_config.json.example job_config.json

# In base_config.json, set API keys and models if needed
```

3) Install required tools

- **pg_hint_plan**: Required to support hint application in PostgreSQL. Please install `pg_hint_plan` extension following the [official documentation](https://github.com/ossc-db/pg_hint_plan).

- **QED (Query Equivalence Decider)**: Required for SQL query equivalence verification. For QED installation, please refer to `install_qed.sh` script:
  ```bash
  bash install_qed.sh
  ```
  Or manually follow the installation steps in the script.

4) Download detection data

- For the diagnosis tool, we provide both the pre-collected training data and the pre-trained RCRank model, which can be downloaded directly from the [link](https://drive.google.com/drive/folders/1mRkA_CJvKImJeHuaXm-W0h0riVmvB_2w?usp=sharing). You can also retrain the model and recollect data using the modified RCRank [code](https://github.com/HarneyHong/RCRank.git) if needed.

- After downloading, please place the data and model files in the following specified paths:
    - `DREAM/src/agent/plan/model_res/GateComDiffPretrainModel slow_sql_data_gen eta0.07/best_model.pt`
    - `DREAM/src/agent/plan/slow_sql_data_gen.csv`

5) Setup Retriever Model

- For the case retriever, we provide a pre-trained Soft Q Retriever model that can be downloaded directly from the [Google Drive](https://drive.google.com/drive/folders/1mRkA_CJvKImJeHuaXm-W0h0riVmvB_2w?usp=sharing). After downloading, place the model file at the path specified in the `retriever_model_path` configuration parameter (default: `src/agent/memory/model`).

- Alternatively, you can retrain the retriever model using collected experience samples:
  ```bash
  cd src/agent/memory/
  python train_retriever_offline.py \
      --samples_path experience/retriever_samples.jsonl \
      --model_save_path model/soft_q_retriever.pt \
      --config_path ../../../config/tpch_config.json \
      --epochs 10 \
      --batch_size 32 \
      --learning_rate 0.0001
  ```

- To collect new experience data from scratch, `set enable_save_samples = true` in the configuration file. The collected samples will be saved to the path specified by `samples_save_path` (default: `src/agent/memory/experience/retriever_samples.jsonl`).

6) Run

```bash
cd src
python main.py \
  --data_path /root/DREAM/data/slow_queries/TPC-H \
  --order qorder.txt \
  --duration 30 \
  --config /root/DREAM/config/tpch_config.json
```