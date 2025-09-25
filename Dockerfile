FROM python:3.12-slim


WORKDIR /app


COPY . .


COPY requirements.txt .


RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 1425


CMD ["python", "bot_next_gen_11.py"]





