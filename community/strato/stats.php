<?php
// GET totals per recipe: reports, how many worked, the median tok/s and the settings changed most often.
// ?recipe=<name> limits it to one recipe. Cached for five minutes. Hidden reports are not counted.

declare(strict_types=1);
require __DIR__ . '/lib.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'GET') {
    header('Allow: GET');
    ts_fail(405, 'GET the totals');
}
$only = isset($_GET['recipe']) ? (string)$_GET['recipe'] : null;
if ($only !== null && !preg_match('/^[a-z0-9][a-z0-9._-]{0,80}$/', $only)) {
    ts_fail(422, 'invalid recipe name');
}

// the full listing is cached (one file); a single recipe's totals are read fresh
$cache = $only === null ? ts_stats_cache() : null;
if ($cache !== null && is_file($cache) && time() - (int)filemtime($cache) < 300) {
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: public, max-age=300');
    readfile($cache);
    exit;
}

function ts_median(array $values): ?float
{
    if (!$values) {
        return null;
    }
    sort($values);
    $n = count($values);
    return $n % 2 ? (float)$values[intdiv($n, 2)] : ($values[$n / 2 - 1] + $values[$n / 2]) / 2.0;
}

$db = ts_db();
$sql = 'SELECT recipe, mode, outcome, tok_s, changes, updated_at FROM reports WHERE hidden = 0';
$args = [];
if ($only !== null) {
    $sql .= ' AND recipe = ?';
    $args[] = $only;
}
try {
    $q = $db->prepare($sql . ' ORDER BY id');
    $q->execute($args);
    $rows = $q->fetchAll();
} catch (PDOException $e) {
    error_log('twinspark: reading the totals failed: ' . $e->getMessage());
    ts_fail(503, 'the totals are not available right now');
}

$out = [];
foreach ($rows as $r) {
    $name = $r['recipe'];
    $s = &$out[$name];
    $s ??= ['reports' => 0, 'works' => 0, 'works_with_changes' => 0, 'fails' => 0, 'tok' => [], 'last' => '',
        'modes' => [], 'pairs' => []];
    $s['reports']++;
    $key = ['works' => 'works', 'works-with-changes' => 'works_with_changes', 'fails' => 'fails'][$r['outcome']] ?? null;
    if ($key !== null) {
        $s[$key]++;
    }
    if ($r['tok_s'] !== null) {
        $s['tok'][] = (float)$r['tok_s'];
    }
    $s['last'] = max($s['last'], (string)$r['updated_at']);
    $mode = $r['mode'] === '' ? '-' : $r['mode'];
    $m = &$s['modes'][$mode];
    $m ??= ['reports' => 0, 'works' => 0, 'fails' => 0, 'tok' => []];
    $m['reports']++;
    if ($r['outcome'] !== 'fails') {
        $m['works']++;
    } else {
        $m['fails']++;
    }
    if ($r['tok_s'] !== null) {
        $m['tok'][] = (float)$r['tok_s'];
    }
    unset($m);
    if ($r['outcome'] !== 'fails') {           // what people changed in setups that work
        foreach (json_decode((string)$r['changes'], true) ?: [] as $c) {
            if (is_array($c) && isset($c['field']) && ts_tuning((string)$c['field']) && ts_plain($c['value'] ?? null)) {
                $pair = json_encode([$c['field'], $c['value'] ?? null]);
                $s['pairs'][$pair] = ($s['pairs'][$pair] ?? 0) + 1;
            }
        }
    }
    unset($s);
}

$recipes = [];
foreach ($out as $name => $s) {
    arsort($s['pairs']);
    $top = [];
    foreach (array_slice($s['pairs'], 0, 10, true) as $pair => $count) {
        [$field, $value] = json_decode($pair, true);
        $top[] = ['field' => $field, 'value' => $value, 'count' => $count];
    }
    $modes = [];
    foreach ($s['modes'] as $mode => $m) {
        $modes[$mode] = ['reports' => $m['reports'], 'works' => $m['works'], 'fails' => $m['fails'],
            'median_tok_s' => ts_median($m['tok'])];
    }
    $recipes[$name] = [
        'reports' => $s['reports'], 'works' => $s['works'], 'works_with_changes' => $s['works_with_changes'],
        'fails' => $s['fails'], 'success_rate' => round(($s['works'] + $s['works_with_changes']) / $s['reports'], 3),
        'median_tok_s' => ts_median($s['tok']), 'last_report' => $s['last'] !== '' ? $s['last'] . 'Z' : null,
        'modes' => $modes, 'top_changes' => $top,
    ];
}
$body = json_encode(['ok' => true, 'generated_at' => gmdate('Y-m-d\TH:i:s\Z'), 'recipes' => (object)$recipes],
    JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
if ($cache !== null) {
    @file_put_contents($cache, $body, LOCK_EX);
}
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: public, max-age=300');
echo $body;
