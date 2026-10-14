FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl nodejs && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir aiogram aiosqlite yt-dlp python-dotenv imageio-ffmpeg
CMD ["python", "bot.py"]
