# Community reports: the server side

Two PHP files and two tables. TwinSpark installations send a report when their user chooses to
(`tsm cookbook report <profile>`, or *Share result* in the Cookbook), and the Cookbook shows the totals per
recipe: how many reports, how many worked, the median tok/s, and the settings people changed most often in
setups that work. What a report contains, and what it never contains, is described in
[docs/cookbook.md](../../docs/cookbook.md#community-results).

Any PHP 8.1+ host with MySQL 8.0 or MariaDB 10.6+ works. These steps are for Strato shared hosting; other
hosts differ only in where things are in the panel.

| File | |
|---|---|
| `report.php` | `POST` a `twinspark-report/1` document (JSON, 16 KiB at most) |
| `stats.php` | `GET` the totals (`?recipe=<name>` for one recipe) |
| `lib.php` | shared code: the checks (the same as `twinspark/cookbook/community.py`), the database |
| `schema.sql` | the two tables |
| `config.example.php` | the configuration, with placeholders |
| `.htaccess` | serves only the two endpoints |
| `private.htaccess` | for the private folder (see step 3) |

## Set it up

1. **A database.** In the Strato panel: *Datenbanken und Webspace → Datenbanken verwalten → Datenbank
   anlegen*. Note the server name, database name and user the panel shows; set a password there.
2. **The tables.** Open phpMyAdmin from the same page, select the database, *Importieren*, choose
   `schema.sql`.
3. **The configuration, outside the web folder.** Your domain points to a folder of your webspace (*Domains
   → Verwalten → Ziel*). Next to that folder (not inside it) create a folder `twinspark-private`, copy
   `config.example.php` into it as `config.php`, fill in the database values and a new random `secret`:

   ```bash
   php -r 'echo bin2hex(random_bytes(32)), PHP_EOL;'
   ```

   Copy `private.htaccess` into the same folder as `.htaccess`, in case it ever becomes reachable.
4. **The endpoint.** Upload `report.php`, `stats.php`, `lib.php` and `.htaccess` into a folder
   `twinspark` inside the domain's folder. The layout is then:

   ```
   <webspace>/
     twinspark-private/config.php      ← not reachable from the web
     twinspark-private/.htaccess
     <domain folder>/twinspark/report.php
     <domain folder>/twinspark/stats.php
     <domain folder>/twinspark/lib.php
     <domain folder>/twinspark/.htaccess
   ```

   If the config has to live elsewhere, set its path in `.htaccess` (`SetEnv TWINSPARK_CONFIG …`).
5. **Check it.** `https://<your-domain>/twinspark/stats.php` answers `{"ok":true,…,"recipes":{}}`;
   `https://<your-domain>/twinspark/lib.php` and `…/schema.sql` answer 403 or 404. Use https: the
   TwinSpark side refuses plain http.
6. **Point TwinSpark at it.** In `/etc/twinspark/controller.yaml` on node A:

   ```yaml
   cookbook:
     community_url: https://<your-domain>/twinspark
   ```

   then `sudo systemctl restart twinspark-controller`. For every installation at once, the default can be
   set in `twinspark/schemas/config.py` (`CookbookSettings.community_url`) in a release.

## Running it

- **Moderation.** A report can be hidden without deleting it; hidden reports are not counted:

  ```sql
  SELECT id, recipe, outcome, comment FROM reports ORDER BY id DESC LIMIT 50;
  UPDATE reports SET hidden = 1 WHERE id = 123;
  ```

- **Limits.** 30 reports per address and day (`max_reports_per_day`); a later report from the same
  installation for the same recipe file and mode replaces its earlier one, so one setup counts once.
  Anonymous reports (`--anonymous`) count each time.
- **What is stored.** The report's fields, a keyed hash (HMAC with `secret`) of the random reporter id, and
  for the rate limit a keyed hash of address and day, deleted after two days. No addresses, host names or
  user names. Changing `secret` makes earlier reporter ids unrecognisable (their next report counts anew).
- **Backups.** phpMyAdmin → *Exportieren*. The data is small: a few hundred bytes per report.
- **Credentials.** The database password exists only in `twinspark-private/config.php` on the server.
  It is never part of this repository, of a TwinSpark installation or of a report.

## Testing it locally

`tests/test_community.py` runs these files with PHP's built-in server and SQLite (`php`, `pdo_sqlite`,
`mbstring`). Against a MySQL/MariaDB **test** database whose tables come from `schema.sql`:

```bash
TSM_TEST_MYSQL_DSN="mysql:host=127.0.0.1;port=3306;dbname=ts_test;charset=utf8mb4" \
TSM_TEST_MYSQL_USER=ts_test TSM_TEST_MYSQL_PASSWORD=… python -m pytest tests/test_community.py
```

The test empties both tables first — never point it at the real database.
