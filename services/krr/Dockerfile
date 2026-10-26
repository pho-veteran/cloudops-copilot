FROM us-central1-docker.pkg.dev/genuine-flight-317411/devel/krr:v1.31.1@sha256:835bf3b6fec4d8f27d96d236a36542bd87bfcb4f12f3537f8dce7f886916ed9c
COPY services/krr/runner.py /cloudops-runner.py
ENTRYPOINT ["python", "/cloudops-runner.py"]
