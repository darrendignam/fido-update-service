# Operations

Host: `build` (`172.104.241.154`), Debian 9, Docker 19.03, standalone `docker-compose` v2.
Stack: `/var/docker-compose-stacks/fidosigs-opf-labs/`, a copy of `deploy/` plus `format/`.

```text
fidosigs-opf-labs/
  docker-compose.yml
  docker-compose.local.yml
  docker-compose.subnet.yml
  .env                  from .env.example: image tag, tunnel token, subnet
  format/vNNN/          live releases, mounted read-only at /data/format
```

The host's Docker default address pools are exhausted, so its `.env` sets
`COMPOSE_FILE=docker-compose.yml:docker-compose.subnet.yml` and
`FIDOSIGS_SUBNET=10.250.10.0/24`. Passing `-f` on the command line replaces `COMPOSE_FILE`,
so include the subnet overlay explicitly:
`docker-compose -f docker-compose.yml -f docker-compose.subnet.yml -f docker-compose.local.yml up -d`.

## First deploy

```bash
cd /var/docker-compose-stacks/fidosigs-opf-labs
cp .env.example .env                  # set FIDOSIGS_TAG, CLOUDFLARE_TUNNEL_TOKEN
docker-compose pull web

# Seed format/ from the releases bundled in the image
docker run --rm --entrypoint tar "$(docker-compose config --images | head -1)" \
  -C /src/fidosigs/resources -c format | tar -x

docker-compose --profile tunnel up -d
```

Cloudflare Zero Trust: tunnel public hostname `fidosigs.opf-labs.org` -> HTTP -> `web:5000`.

GHCR packages start private. Either set the package public (GitHub -> Packages ->
fidosigs -> Package settings -> Change visibility) or `docker login ghcr.io` on the host
with a `read:packages` token.

## Verify

```bash
curl -s https://fidosigs.opf-labs.org/format/latest/          # <signature version="vNNN" />
curl -sI https://fidosigs.opf-labs.org/format/latest/ | head -1  # 200, never 307
docker-compose ps                                             # web: healthy
```

With the real client (fido 1.6.1):

```bash
python -m venv /tmp/fido && /tmp/fido/bin/pip install opf-fido==1.6.1
conf=$(/tmp/fido/bin/python -c 'import fido; print(fido.CONFIG_DIR)')
sed -i 's#<updateSite>.*</updateSite>#<updateSite>https://fidosigs.opf-labs.org</updateSite>#' "$conf/versions.xml"
/tmp/fido/bin/fido -sigs check     # Updated signatures vNNN are available ...
/tmp/fido/bin/fido -sigs update    # writes formats-vNNN.xml, updates versions.xml
```

## Publish a new PRONOM release

PRONOM releases a few times a year. `check-live` goes red within a week of one.

1. **Generate.** `update-signatures` runs monthly and opens a PR `signatures/vNNN`. To run
   it now: Actions -> update-signatures -> Run workflow (version blank = latest). Or locally:

   ```bash
   pip install -r scripts/requirements.txt
   python scripts/generate_signatures.py --format-dir fidosigs/resources/format --work-dir .work
   # exit 0 built, 2 already present, 1 PRONOM failure; rerun resumes from .work
   ```

2. **Merge the PR.** `publish` tests it and pushes `latest` and `pronom-vNNN`.

3. **Copy the release onto the host.** No restart: the service lists `format/` per request.
   Extract to a hidden name and rename, so clients never see a partial release:

   ```bash
   cd /var/docker-compose-stacks/fidosigs-opf-labs
   docker pull ghcr.io/darrendignam/fidosigs:latest
   mkdir .incoming && docker run --rm --entrypoint tar ghcr.io/darrendignam/fidosigs:latest \
     -C /src/fidosigs/resources/format -c vNNN | tar -x -C .incoming
   mv .incoming/vNNN format/ && rmdir .incoming
   curl -s https://fidosigs.opf-labs.org/format/latest/
   ```

## Release code

```bash
git tag -a v1.1.0 -m "..." && git push origin v1.1.0      # publish pushes :1.1.0 and :1.1
```

On the host: set `FIDOSIGS_TAG=1.1.0` in `.env`, then `docker-compose pull web && docker-compose --profile tunnel up -d`.

## Roll back

- Code: previous `FIDOSIGS_TAG`, then `pull` and `up -d`.
- Data: `mv format/vNNN .withdrawn-vNNN`. `/format/latest` falls back at once.

## Production cutover (fidosigs.openpreservation.org)

Not done. Today that hostname reaches the 2022 container `fidosigs` (`openpreserve/fidosigs`,
host port 1967, watchtower-enabled) through the edge proxy. To cut over, route the hostname
to this stack (a second public hostname on the same tunnel, `-> web:5000`), confirm
`/format/latest`, then stop the old container. Keep its image for rollback. Do not push to
`openpreserve/fidosigs` on Docker Hub: watchtower on this host would redeploy it.

## Moving the repo to openpreserve

Workflows use `github.repository_owner`, so the image becomes
`ghcr.io/openpreserve/fidosigs` with no edits. Change `FIDOSIGS_IMAGE` in `.env`, the
default in `deploy/docker-compose.yml`, and set the repository variable `FIDOSIGS_URL` if
`check-live` should watch the production hostname.
