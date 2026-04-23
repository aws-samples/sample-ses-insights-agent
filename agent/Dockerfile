FROM public.ecr.aws/docker/library/python:3.11-slim

RUN apt-get update && \
    apt-get install -y gcc g++ python3-dev curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agentcore_runtime.py .

# Verify imports at build time
RUN python -c "from bedrock_agentcore.runtime import BedrockAgentCoreApp; print('OK')" && \
    python -c "from strands import Agent; print('OK')" && \
    python -c "from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client; print('OK')" && \
    python -c "from strands_tools.code_interpreter import AgentCoreCodeInterpreter; print('OK')"

ENV PYTHONPATH="/app"
ENV AWS_DEFAULT_REGION=us-east-1
ENV OTEL_SERVICE_NAME=ses-analytics-agent
ENV OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/ping || exit 1

CMD ["opentelemetry-instrument", "python", "agentcore_runtime.py"]
