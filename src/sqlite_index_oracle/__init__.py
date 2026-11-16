"""sqlite-index-oracle: verified index advice for SQLite workloads."""
from .oracle import Advice, Candidate, advise, extract_columns, read_workload

__all__ = ["Advice", "Candidate", "advise", "extract_columns", "read_workload"]
__version__ = "0.1.0"
