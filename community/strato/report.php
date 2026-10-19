<?php
// POST a twinspark-report/1 document (JSON). One report per reporter, recipe file and mode: a later one
// replaces the earlier. See README.md.

declare(strict_types=1);
require __DIR__ . '/lib.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    header('Allow: POST');
    ts_fail(405, 'POST a report');
}
if (stripos((string)($_SERVER['CONTENT_TYPE'] ?? ''), 'application/json') !== 0) {
    ts_fail(415, 'send the report as application/json');
}
$raw = file_get_contents('php://input', false, null, 0, TS_MAX_BODY + 1);
if ($raw === false || strlen($raw) > TS_MAX_BODY) {
    ts_fail(413, 'the report is larger than 16 KiB');
}
try {
    $doc = ts_check_report(json_decode($raw, true, 8, JSON_THROW_ON_ERROR));
} catch (JsonException) {
    ts_fail(400, 'the report is not valid JSON');
} catch (InvalidArgumentException $e) {
    ts_fail(422, $e->getMessage());
}

$db = ts_db();
$today = gmdate('Y-m-d');
$now = gmdate('Y-m-d H:i:s');
$ip = ts_hash((string)($_SERVER['REMOTE_ADDR'] ?? '') . '|' . $today);
$limit = (int)(ts_config()['max_reports_per_day'] ?? 30);

$db->beginTransaction();
try {
    $q = $db->prepare('SELECT hits FROM rate_limits WHERE ip_hash = ? AND day = ?');
    $q->execute([$ip, $today]);
    $hits = $q->fetchColumn();
    if ($hits === false) {
        $db->prepare('INSERT INTO rate_limits (ip_hash, day, hits) VALUES (?, ?, 1)')->execute([$ip, $today]);
    } elseif ((int)$hits >= $limit) {
        $db->rollBack();
        header('Retry-After: 86400');
        ts_fail(429, 'too many reports from this address today');
    } else {
        $db->prepare('UPDATE rate_limits SET hits = hits + 1 WHERE ip_hash = ? AND day = ?')->execute([$ip, $today]);
    }
    if (random_int(1, 50) === 1) {      // now and then: forget the counters of earlier days
        $db->prepare('DELETE FROM rate_limits WHERE day < ?')->execute([gmdate('Y-m-d', time() - 2 * 86400)]);
    }

    $reporter = isset($doc['reporter']) ? ts_hash('reporter|' . $doc['reporter']) : null;
    $mode = (string)($doc['mode'] ?? '');
    $tok = $doc['benchmarks']['max-throughput']['peak_output_tok_s'] ?? null;
    $row = [
        'outcome' => $doc['outcome'],
        'failure_stage' => $doc['outcome'] === 'fails' ? ($doc['failure_stage'] ?? 'other') : null,
        'twinspark' => (string)$doc['twinspark'],
        'engine' => $doc['engine'],
        'engine_version' => $doc['engine_version'] ?? null,
        'topology' => $doc['topology'] ?? null,
        'tok_s' => is_int($tok) || is_float($tok) ? (float)$tok : null,
        'benchmarks' => json_encode((object)$doc['benchmarks'], JSON_UNESCAPED_SLASHES),
        'changes' => json_encode($doc['changes'], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE),
        'comment' => trim($doc['comment']),
    ];
    $existing = false;
    if ($reporter !== null) {
        $q = $db->prepare('SELECT id FROM reports WHERE reporter_hash = ? AND recipe = ? AND recipe_sha256 = ?'
            . ' AND mode = ?');
        $q->execute([$reporter, $doc['recipe'], $doc['recipe_sha256'], $mode]);
        $existing = $q->fetchColumn();
    }
    if ($existing !== false) {
        $sets = implode(', ', array_map(static fn($k) => "$k = :$k", array_keys($row)));
        $db->prepare("UPDATE reports SET $sets, updated_at = :now WHERE id = :id")
            ->execute($row + ['now' => $now, 'id' => $existing]);
        $message = 'your earlier report of this recipe was replaced';
    } else {
        $cols = array_merge(['received_at', 'updated_at', 'reporter_hash', 'recipe', 'recipe_sha256', 'mode'],
            array_keys($row));
        $db->prepare('INSERT INTO reports (' . implode(', ', $cols) . ') VALUES (:'
            . implode(', :', $cols) . ')')
            ->execute(['received_at' => $now, 'updated_at' => $now, 'reporter_hash' => $reporter,
                'recipe' => $doc['recipe'], 'recipe_sha256' => $doc['recipe_sha256'], 'mode' => $mode] + $row);
        $message = 'recorded';
    }
    $db->commit();
} catch (PDOException $e) {
    if ($db->inTransaction()) {
        $db->rollBack();
    }
    error_log('twinspark: storing a report failed: ' . $e->getMessage());
    ts_fail(503, 'the report could not be stored right now');
}
@unlink(ts_stats_cache());                  // the next totals include this report
ts_json($existing !== false ? 200 : 201, ['ok' => true, 'message' => $message]);
