# The Playwright engine and both HTTP transports in one image, for a CI
# canary or a scheduled job. Not needed for local development.
#
#   docker build -t potterybarn-scraper .
#   docker run --rm -v "$PWD/out:/out" potterybarn-scraper \
#       api_scraper.py --group-id sofa --all-pages --out /out/sofas
#   docker run --rm -v "$PWD/out:/out" --env-file .env potterybarn-scraper \
#       http_scraper.py --url https://www.potterybarn.com/products/delaney-marble-end-table/ --out /out/delaney
#
# Credentials come in through --env-file. NOTHING here bakes one in: a .env
# baked into an image is a credential published to everyone who can pull it.
FROM python:3.14-slim

WORKDIR /app

# curl is the product-page client — python-requests was refused 5 of 6 times
# where curl got the page on 6 of 6 (2026-09-24). It is not in -slim.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-playwright.txt ./
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

# Every module the entrypoints import, transitively. smoke_test.py checks this
# list against the real import graph: this family has shipped an image that
# died with ModuleNotFoundError on every invocation, --help included.
COPY api_scraper.py browser_bridge.py captcha_solver.py catalog_walk.py \
     diff_runs.py env_config.py fingerprint_client.py http_scraper.py \
     output_writer.py page_flow.py playwright_scraper.py product_parser.py \
     proxy_pool.py ./

# Not root. Playwright's browsers were installed above into the shared cache
# under /ms-playwright, so the unprivileged user can launch them.
RUN useradd --create-home --uid 10001 scraper && mkdir -p /out && chown scraper /out
USER scraper

ENTRYPOINT ["python3"]
CMD ["api_scraper.py", "--help"]
