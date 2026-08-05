from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeOptions:
    verbose: bool = False
