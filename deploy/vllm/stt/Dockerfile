FROM vllm/vllm-openai:v0.21.0

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir -U \
      av \
      soundfile \
      librosa \
      ffmpeg-python
