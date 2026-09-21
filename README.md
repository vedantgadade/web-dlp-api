# VGSAVE Fresh

A clean FastAPI + yt-dlp public-media downloader.

## Run
```bash
docker compose up --build
```
Open http://localhost:8000

Only download public/authorized content. The service does not bypass DRM, private content, login walls, or access controls.

## Environment
`POT_PROVIDER_URL` is optional. If supplied, it should point to a compatible bgutil yt-dlp PO-token provider, for example:
`http://bgutil-ytdlp-pot-provider:4416`
