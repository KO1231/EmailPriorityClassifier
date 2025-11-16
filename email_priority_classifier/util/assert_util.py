def assert_positive(value, var_name: str):
    if value <= 0:
        raise ValueError(f"{var_name} must be a positive integer, got {value}")
    return value
