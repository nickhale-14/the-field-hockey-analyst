FROM python:3.12-slim

WORKDIR /home/jovyan/work

RUN pip install --no-cache-dir \
    jupyterlab \
    pandas \
    scipy \
    sqlalchemy \
    pymysql \
    matplotlib

EXPOSE 8888

CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--IdentityProvider.token="]
