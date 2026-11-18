# Base image with Python preinstalled
FROM apify/actor-python:3.13

USER myuser

# Copy just requirements.txt first so dependency installs are cached
COPY --chown=myuser:myuser requirements.txt ./

RUN echo "Python version:" \
 && python --version \
 && echo "Pip version:" \
 && pip --version \
 && echo "Installing dependencies:" \
 && pip install -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

# Copy the rest of the source code
COPY --chown=myuser:myuser . ./

# Ensure the Actor Python code is runnable
RUN python3 -m compileall -q src/

# Launch the Actor entry point
CMD ["python3", "-m", "src"]
