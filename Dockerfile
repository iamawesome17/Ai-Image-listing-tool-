FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/share/fonts/truetype/google-fonts
COPY fonts/*.ttf /usr/share/fonts/truetype/google-fonts/
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=7860 PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["python", "app.py"]
