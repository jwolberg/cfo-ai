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
#
# --proxy-headers because Cloud Run terminates TLS in front of the container and forwards
# plain HTTP. Without it Uvicorn believes the request scheme is http, and anything FastAPI
# builds from it is wrong: a redirect off `GET /decisions/` emitted `Location: http://…`,
# silently downgrading the client to plaintext. Observed on the deployed service.
#
# --forwarded-allow-ips='*' is what makes --proxy-headers take effect: Uvicorn only trusts
# X-Forwarded-* from allowed peers, and the Cloud Run proxy's address is not knowable ahead
# of time. Trusting any peer is safe *here* precisely because nothing but Cloud Run can reach
# the container — it holds no public port of its own, so there is no unproxied path by which
# a forged X-Forwarded-Proto could arrive. Do not carry this flag to a host that is directly
# reachable.
web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers --forwarded-allow-ips='*'
