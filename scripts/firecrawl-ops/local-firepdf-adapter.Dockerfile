FROM python:3.12-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends poppler-utils \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scripts/firecrawl-ops/local_firepdf_ocr_service.py /app/local_firepdf_ocr_service.py
COPY scripts/firecrawl-ops/pdf_ocr_profiles.json /app/pdf_ocr_profiles.json

ENV LOCAL_FIREPDF_HOST=0.0.0.0
ENV LOCAL_FIREPDF_PORT=31337
ENV LOCAL_FIREPDF_ENGINE=docling
ENV LOCAL_FIREPDF_DOCLING_URL=http://host.docker.internal:5001

EXPOSE 31337

CMD ["python", "/app/local_firepdf_ocr_service.py"]
