FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy evaluation script and data
COPY ragas_evaluation.py /app/ragas_evaluation.py
COPY README.md /app/README.md
COPY ragas_data.xlsx /app/ragas_data.xlsx

# Output directory for results
RUN mkdir -p /app/output

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "ragas_evaluation.py"]
CMD ["--output", "/app/output/ragas_results.xlsx"]
