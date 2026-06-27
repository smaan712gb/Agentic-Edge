"""Offline research harness for Agentic Edge.

This package holds *offline*, read-only tools that replay the system's own
decision audit log against realised outcomes to validate and tune the
hand-set parameters that drive live trading (exit-pressure weights, band
thresholds, ...).

Design lineage: Karpathy's ``autoresearch`` loop — mutate one small set of
parameters, evaluate against a *fixed, deterministic* metric, keep only what
beats the incumbent out-of-sample. Here the "5-minute training run" is a
backtest over historical ``auto_actions`` + ``positions`` snapshots, and the
fitness metric is how well the resulting exit-pressure score separates
forward winners from forward losers.

Nothing in this package touches the broker, places orders, or mutates live
config. The optimiser *recommends*; promotion to production is a human gate
(real money).
"""
