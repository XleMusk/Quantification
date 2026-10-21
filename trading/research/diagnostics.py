"""Post-run explanation using frozen models; never training or factor selection."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .experiment import write_json
from .factors import LABEL, daily_ic, ic_summary


def run(folder):
    folder = Path(folder)
    record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    if record["status"] != "completed":
        raise ValueError("Only explain a completed run")
    if record.get("experiment_kind") == "alpha158_baseline_comparison":
        raise ValueError("This diagnostic requires a B1 factor-search run; baseline-only has no selected factors")
    selected = record["selection"]["selected"]
    source = Path(record["data_output"])
    values = pd.read_parquet(source / "features_and_candidates.parquet")
    dates = values.index.get_level_values("date")
    start, end = record["config"]["segments"]["test"]
    values = values.loc[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    labels = pd.read_parquet(source / "labels.parquet").reindex(values.index)
    label = labels[LABEL].where(labels.exit_date.le(pd.Timestamp(end)))
    usage, sensitivity = [], []
    for seed in record["config"]["seeds"]:
        model_folder = source / f"B1_seed{seed}"
        model = lgb.Booster(model_file=str(model_folder / "model.txt"))
        medians = pd.read_csv(model_folder / "preprocessing.csv", index_col=0).iloc[:, 0]
        x = values[model.feature_name()].fillna(medians)
        original = model.predict(x)
        saved = pd.read_parquet(model_folder / "historical_scores.parquet").set_index(["datetime", "instrument"]).score
        np.testing.assert_allclose(original, saved.reindex(values.index).to_numpy(), rtol=1e-9, atol=1e-12)
        splits, gains = model.feature_importance("split"), model.feature_importance("gain")
        for name, split, gain in zip(model.feature_name(), splits, gains):
            usage.append({"seed": seed, "feature": name, "is_new_factor": name in selected,
                          "tree_count": model.num_trees(), "splits": int(split), "gain": float(gain)})
        for name in selected + ["ALL_SELECTED"]:
            altered = x.copy()
            columns = selected if name == "ALL_SELECTED" else [name]
            altered[columns] = medians[columns].to_numpy()
            changed = model.predict(altered)
            ic = daily_ic(pd.DataFrame({"score": changed}, index=x.index), label, record["config"]["min_pairs"]).score
            sensitivity.append({"seed": seed, "replaced_with_training_median": name,
                                "changed_score_rows": int((np.abs(changed - original) > 1e-12).sum()),
                                "total_score_rows": len(x), "mean_absolute_score_change": float(np.abs(changed - original).mean()),
                                **ic_summary(ic)})
    pd.DataFrame(usage).to_csv(folder / "frozen_model_feature_usage.csv", index=False)
    pd.DataFrame(sensitivity).to_csv(folder / "frozen_model_sensitivity.csv", index=False)
    factor_ic = pd.read_csv(folder / "selected_and_alpha158_test_daily_ic.csv", index_col=0)
    factor_summary = []
    for item in record["selection"]["selected_formulas"]:
        factor_summary.append({"name": item["name"], "formula": item["formula"],
                               **ic_summary(factor_ic[item["name"]] * item["direction_from_train"])})
    pd.DataFrame(factor_summary).to_csv(folder / "selected_factor_test_summary.csv", index=False)
    usage_frame = pd.DataFrame(usage)
    totals = usage_frame.loc[usage_frame.is_new_factor].groupby("feature").splits.sum()
    unused = totals.index[totals == 0].tolist()
    zero_seeds = usage_frame.loc[usage_frame.is_new_factor].groupby("seed").splits.sum()
    zero_seeds = zero_seeds.reindex(record["config"]["seeds"], fill_value=0)
    model_metrics = pd.DataFrame(record["model_metrics"])
    validation = model_metrics.loc[model_metrics.split.eq("valid")].pivot(index="seed", columns="variant", values="mean_rank_ic")
    validation_gains = (validation.B1 - validation.B0).to_dict()
    trees = usage_frame.groupby("seed").tree_count.first().to_dict()
    similarity_lines = []
    if selected:
        trials = pd.read_csv(folder / "selection_trials.csv")
        for row in trials.loc[trials.accepted].itertuples():
            similarity_lines.append(f"- {row.name}最接近的原特征为{row.nearest_alpha158}，训练期截面排名相似度{row.pooled_daily_rank_similarity:.3f}；这不是独立经济信息的证明。")
    deltas = pd.DataFrame(record["paired_deltas"])
    review = {"kind": "post_hoc_frozen_model_diagnostic_not_new_holdout_or_selection",
              "selection_sha256": sha256((folder / "selection_frozen.json").read_bytes()).hexdigest(),
              "models_retrained": False, "selection_changed": False, "unused_new_factors": unused,
              "seeds_using_no_new_factor": zero_seeds.index[zero_seeds == 0].tolist(),
              "validation_gains_by_seed": validation_gains, "b1_trees_by_seed": trees,
              "mean_delta_test_ic": float(deltas.delta_test_ic.mean()),
              "mean_delta_net_return": float(deltas.delta_net_return.mean()),
              "net_improved_seeds": int(deltas.delta_net_return.gt(0).sum()),
              "model_prediction_reproduction": "passed_all_seeds", "deployment": "not_promoted"}
    write_json(folder / "post_run_review.json", review)
    lines = ["# 固定模型检查与结果解释", "",
             "这是看完结果后的诊断，不是新的未见测试；没有重训、重选、改变运行策略或修改同花顺。", "",
             f"B1-B0 的平均测试 Rank IC 差为 {review['mean_delta_test_ic']:+.5f}，净收益差为 {review['mean_delta_net_return']:+.2%}。",
             f"仅 {review['net_improved_seeds']}/{len(record['config']['seeds'])} 个种子的净收益改善。",
             "本轮没有提供足以替换原策略的新增因子证据；不部署这两个候选。", "",
             "## 模型实际使用", "",
             f"- 三个最终模型完全没用到的新因子：{', '.join(unused) or '无'}。",
             f"- 没有使用任何新增因子的模型种子：{review['seeds_using_no_new_factor']}。",
             f"- B1最终保留树数（种子:树数）：{trees}。这是验证早停的实际结果，没有强行增加训练轮数。",
             "- 新增特征即使未成为最终分裂，也可能改变列采样/树搜索过程；B0与B1预测不同不能单独证明新增信息。",
             f"- 每个种子的验证IC增量：{validation_gains}。筛选只使用第一个种子，不能将其他种子的下降略去。",
             *similarity_lines, "",
             "## 固定输入诊断", "",
             "加载保存的最终模型，先逐行重现原测试预测，再把新增因子替换成各自的训练期中位数。",
             "这是对模型依赖的诊断，不是重新训练的因果消融或新的策略收益结论。详见 frozen_model_sensitivity.csv。", "",
             "## 保留与下一步", "",
             "保留全部200候选、筛选失败记录、实际公式、逐日IC、逐种子预测和成交账本。",
             "先补具备历史构成与状态信息的更大股票池，再预先指定新的评价窗口；不继续针对已看的2026测试结果调公式。"]
    (folder / "POST_RUN_REVIEW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(review, ensure_ascii=False, indent=2))
    return review


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    run(parser.parse_args().run)
