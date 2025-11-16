# EmailPriorityClassifier

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