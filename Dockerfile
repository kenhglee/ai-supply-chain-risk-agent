FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt .
RUN pip install -r requirements.txt -t ${LAMBDA_TASK_ROOT}

COPY . ${LAMBDA_TASK_ROOT}

CMD ["supplier_risk_agent.lambda_handler"]
