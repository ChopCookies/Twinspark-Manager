<?php
// TwinSpark community endpoint — configuration.
//
// Copy this file to a folder that is NOT reachable from the web (outside the folder your domain points
// to), name it config.php and fill it in there. lib.php looks for it in this order:
//   1. the path in the environment variable TWINSPARK_CONFIG (e.g. SetEnv in .htaccess)
//   2. ../../twinspark-private/config.php   (API in <docroot>/twinspark/, config next to <docroot>)
// The endpoint writes its cached totals (stats-cache.json) into the same folder.
// Never commit the filled-in file, and never put it into the web folder.

return [
    // the database from the hosting panel (Strato: "Datenbanken" → MySQL/MariaDB)
    'db_host' => 'database-host.example',      // e.g. the server name the panel shows
    'db_name' => 'database-name',
    'db_user' => 'database-user',
    'db_pass' => 'change-me',
    // a long random string; it keys the hashes of addresses and reporter ids. Make one with:
    //   php -r 'echo bin2hex(random_bytes(32)), PHP_EOL;'
    'secret'  => 'replace-with-64-random-hex-characters',
    // reports per address and day
    'max_reports_per_day' => 30,
    // optional: a full PDO DSN instead of db_host/db_name (used by the tests with SQLite)
    // 'dsn' => 'mysql:host=…;dbname=…;charset=utf8mb4',
];
