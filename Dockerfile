# Use ROCm base image for AMD GPU support
FROM rocm/dev-ubuntu-22.04:5.4.2

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    python3-pip \
    python3-dev \
    rocm-libs \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first
COPY requirements.txt .

# Install PyTorch with ROCm
RUN pip3 install --no-cache-dir torch==2.0.1+rocm5.4.2 torchaudio==2.0.2+rocm5.4.2 --index-url https://download.pytorch.org/whl/rocm5.4.2

# Install AMD SMI and other Python packages
RUN pip3 install --no-cache-dir amdsmi numpy==1.24.3 pandas scipy

# Install other requirements
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create necessary directories
RUN mkdir -p uploads processed amd_metrics

# Set ROCm visibility
ENV ROCR_VISIBLE_DEVICES=0
ENV PYTHONUNBUFFERED=1

EXPOSE 7860

CMD ["python3", "app.py"]
