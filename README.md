# EmailPriorityClassifier
EmailPriorityClassifier intelligently evaluates incoming emails and threads to determine how urgently they require user attention.

By analyzing subjects, senders, message bodies, Gmail’s native classifications, and user-assigned labels, it generates a priority score and assigns each message a clear Priority label (P1–P3).
The ability to freely configure system prompts allows for flexible labeling and priority logic (e.g., always assigning emails from specific topics or senders to P1).

This application supports both OpenAI models and OpenAI-compatible local LLMs such as those running through LM Studio, allowing users to choose between high-efficiency cloud models and fully private, on-device inference.
This flexibility enables streamlined inbox triage while meeting diverse performance and privacy requirements.

<img width="756" height="411" alt="email_priority_classifier" src="https://github.com/user-attachments/assets/ed5db17a-f8bb-45d2-94da-9ceae2ae6298" />

## Requirements

- Python 3.13.5
    - pyenv (recommended)
    - pipenv

## Installation

1. (If you use pyenv, and did not install Python 3.13.5 yet)
   ```bash
   pyenv install 3.13.5
   ```

2. Clone this repository
   ```bash
    git clone https://github.com/KO1231/EmailPriorityClassifier.git
    cd EmailPriorityClassifier
    ```

3. Install dependencies using pipenv
   ```bash
   pip3 install pipenv
   pipenv install
   ```

4. Prepare Secrets and configuration
    - Create a `.env` file in the project root directory based on the `.env.example` file.
    - Create a `config.yml` file in the project root directory based on the `config.yml.example` file.
    - Create a `client_secrets.json` file in the project root directory for Gmail API credentials (for Desktop Application).

5. Create Oauth2 tokens
   ```bash
   pipenv run login_google
   ```

## Usage

```bash
pipenv run start
```
