# LumenBench Optical Measurements

Authors: Mina Example; Jian Example
Year: 2026
Version: demonstration draft
Venue: Fictional Measurement Workshop

## Abstract

This is a synthetic paper for demonstrating the Skill. The models, people, dataset, and results are invented.

## Methods

LumenBench contains 48 daylight scenes. LumenBench contains 16 dusk scenes. Model Cedar was trained on Dataset Juniper. Model Birch was evaluated on the held-out LumenBench split. Model Cedar uses temperature scaling.

## Results

On the held-out LumenBench split, Model Cedar achieved 92% accuracy. Model Birch achieved 84% accuracy on the held-out LumenBench split. Model Cedar achieved 76% recall on the held-out LumenBench split. Model Birch achieved 70% recall on the held-out LumenBench split. Model Cedar had a higher F1 score than Model Birch on the held-out LumenBench split.

Model Cedar had a latency of 24 ms at batch size 1 on a CPU. Model Birch had a latency of 32 ms at batch size 1 on a CPU. The median calibration error did not differ significantly between Preset P and Preset Q at the 5% significance level.

Lower light was associated with more false positives in the dusk subset. Only under the fixed-input condition, Filter Indigo reduced measurement noise by 18%. For event E9, Sensor Amber detected a reflected pulse.

The fixed exposure setting may reduce sensor drift over four hours. The baseline parser used 96 MB of memory in the ablation.

Table 1: Recall in the dusk split, with rows from a separate ablation.

| Model | Recall | Split |
|---|---|---|
| Model Larch | 68% | Dusk split |
| Model Alder | 61% | Dusk split |

## Discussion

We hypothesize that lens haze increases capture variance in dusk scenes.

## References

[1] Hart Example. A fictional comparison of optics benchmarks. 2024. DOI: 10.9999/example.2024.1010
