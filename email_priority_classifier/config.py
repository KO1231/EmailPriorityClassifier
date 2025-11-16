from typing import NamedTuple

import yaml

from email_priority_classifier.type.priority import EmailPriority


class EmailPriorityClassifierConfig(NamedTuple):
    label_id: dict[EmailPriority, str]
    priority_labels: dict[EmailPriority, str]


# Load Function
def load_config(path: str) -> EmailPriorityClassifierConfig:
    with open(path, "r") as f:
        raw_config = yaml.safe_load(f)

    return EmailPriorityClassifierConfig(
        label_id={priority: str(raw_config["labelID"][priority.name]) for priority in EmailPriority},
        priority_labels={priority: str(raw_config["priorityLabels"][priority.name]) for priority in EmailPriority}
    )


if __name__ == "__main__":
    from pathlib import Path

    CONFIG_FILE = Path(__file__).resolve().parent.parent / "config.yml"
    config = load_config(str(CONFIG_FILE))
    print(config)
