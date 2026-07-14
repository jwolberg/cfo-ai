# The Cloud Run entrypoint (ticket 0009).
#
# Python buildpacks cannot infer this. Node has a conventional `npm start`; Python has no
# equivalent, so without a Procfile (or a GOOGLE_ENTRYPOINT build var) `gcloud run deploy
# --source .` builds an image that has no idea how to start the app.
#
# --host 0.0.0.0 because Cloud Run health-checks the container from outside it; a default
# bind to 127.0.0.1 is reachable only from within the container and reads as a dead service.
#
# --port $PORT because Cloud Run chooses the port and injects it. Hard-coding 8000 works
# right up until it doesn't.
#
# --workers 1 is not a throughput compromise, it is the same decision as `--max-instances=1`.
# The rate cap on /assistant/message (backend/assistant.py) lives in process memory, and it
# is the only thing bounding Anthropic spend if the public API key leaks. A second worker is
# a second counter and a doubled ceiling. One process, one cap. See docs/tickets/0009.
web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1
