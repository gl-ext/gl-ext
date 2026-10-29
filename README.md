# Demonlist + Pointercrate Records (prototype)

Browser extension prototype for Firefox and Chromium. It tries to append Pointercrate records to the `.record-card` list on demonlist.org, sort cards by progress, and provides a persistent toggle.

## Install in Firefox

1. Extract the ZIP.
2. Open `about:debugging#/runtime/this-firefox`.
3. Click **Load Temporary Add-on…**.
4. Select `manifest.json` from the extracted folder.
5. Open a Demonlist level page and reload it.

Temporary add-ons are removed when Firefox closes; this is normal for local testing.

## Install in Chrome / Chromium

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the extracted folder.

## Important prototype limitations

- This is a first integration prototype, not yet verified against every live Demonlist route or current Pointercrate response.
- The level name is guessed from common heading selectors. If the name is not found or the Pointercrate match is ambiguous, the extension intentionally refuses to merge records.
- It uses the documented Pointercrate API (`/api/v2/demons/` and `/api/v1/records/`) and follows `Link` pagination headers when provided.
- It currently tries to match records by player name + progress to avoid duplicates. It does not modify the site's backend.
- The markup supplied by the user shows `.record-card`, `.record-row`, `.record-player`, `.record-username`, `.record-info`, `.record-percent`, and `.record-video`; the extension reuses these classes for visual consistency.
- The site's parent container and heading selectors may need adjustment after testing on a real level page. If it says it could not find a level, send the level page URL and the surrounding HTML around the title and records list.
- It may not show a player's flag because Pointercrate's API record object may not include nationality in the same shape as Demonlist.

## API docs

- https://github.com/stadust/pointercrate-documentation/blob/master/000_index/000_api.md
- https://github.com/stadust/pointercrate-documentation/blob/master/000_index/300_pagination.md

## Privacy

The extension does not collect or send user browsing data to a third party. It makes public GET requests to Pointercrate. The toggle preference is stored with extension local storage.


## v0.1.2 styling fix

Injected record cards are now cloned from an actual `.record-card` in the live page, retaining Demonlist's generated Svelte scope classes and native styles. The source label row was removed to keep the card height close to native cards. The cloned player's flag is removed to avoid showing the original player's nationality on someone else.
