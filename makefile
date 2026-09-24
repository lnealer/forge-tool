# Variables
PYTHON = python3
PIP = pip
STREAMLIT = streamlit

install: ## Install dependencies from requirements.txt
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

run: ## Launch the Streamlit application locally
	$(STREAMLIT) run python/chat.py 

clean: ## Remove Python cache and virtual environment files
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -delete
	rm -rf .pytest_cache .coverage

