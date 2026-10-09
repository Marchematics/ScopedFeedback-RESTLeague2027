FROM python:3.12-slim
WORKDIR /tool
COPY requirements.txt /tool/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY restleague /tool/restleague
RUN mkdir -p /output /specifications
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "restleague.entrypoint"]
