# DB Tools
This folder contains tools to crawl measurements from the supported databases, to process them, create results and
indexes of results.

The notebook `db.ipynb` should have everything you need and the instructions on running stuff

```shell
jupyter lab db.ipynb
```

## squig.link
`squig_update.ipynb` contains the squig.link workflow only. Use it when only squig.link measurements need to be
updated: the shared import cell in `db.ipynb` imports the oratory1990 crawler, which requires a system Ghostscript
installation that squig.link does not need.

squig.link no longer serves HTML listings of the `data/` directories. `SquigCrawler` therefore reads the site
`phone_book.json` and `config.js` (channels and number of samples) and probes the data directory for the measurement
files instead of scraping a directory listing.

```shell
jupyter lab squig_update.ipynb
```

