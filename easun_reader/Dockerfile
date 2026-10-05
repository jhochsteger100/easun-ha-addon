FROM python:3.11-alpine

RUN pip install --no-cache-dir pyserial paho-mqtt

COPY easun_reader.py /easun_reader.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
