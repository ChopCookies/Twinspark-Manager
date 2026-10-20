<?php
// TwinSpark community endpoint — shared code for report.php and stats.php. Not meant to be opened directly
// (.htaccess refuses it). The rules for a report are the same as in twinspark/cookbook/community.py.

declare(strict_types=1);

// never show PHP's own messages (they would name server paths and break the JSON answer); log them instead
ini_set('display_errors', '0');
ini_set('log_errors', '1');

if (basename($_SERVER['SCRIPT_FILENAME'] ?? '') === 'lib.php') {
    http_response_code(404);
    exit;
}

const TS_FORMAT = 'twinspark-report/1';
const TS_OUTCOMES = ['works', 'works-with-changes', 'fails'];
const TS_STAGES = ['download', 'pull', 'plan', 'start', 'health', 'smoke', 'serving', 'other'];
const TS_PRESETS = ['max-throughput', 'latency-slo', 'context', 'quality', 'fixed'];
const TS_BENCH_FIELDS = ['peak_output_tok_s', 'at_concurrency', 'knee_output_tok_s', 'ttft_p95_ms', 'tpot_p95_ms',
    'max_concurrency', 'output_tok_s', 'longest', 'needle_score_min', 'needle_score', 'tools_ok'];
const TS_TUNING = ['simple.context_length', 'simple.memory_strategy', 'simple.concurrency', 'simple.thinking',
    'simple.tool_calling', 'simple.quantization', 'simple.topology',
    'behaviour.reasoning_parser', 'behaviour.tool_call_parser', 'behaviour.temperature', 'behaviour.top_p',
    'behaviour.top_k', 'behaviour.min_p', 'behaviour.repetition_penalty', 'behaviour.max_tokens',
    'advanced.dtype', 'advanced.kv_dtype', 'advanced.block_size', 'advanced.attention_backend',
    'advanced.weight_loader', 'advanced.tokenizer_mode', 'advanced.eager_mode', 'advanced.chunked_prefill',
    'advanced.prefix_cache', 'advanced.max_num_seqs', 'advanced.max_num_batched_tokens',
    'advanced.gpu_memory_utilization', 'advanced.head_start_delay_s',
    'advanced.speculative_config.method', 'advanced.speculative_config.num_speculative_tokens',
    'engine', 'distributed_backend'];
// fields whose words come from a fixed list (the same lists as ENUMS in community.py)
const TS_ENUMS = [
    'simple.topology' => ['single-a', 'single-b', 'tp2', 'pp2', 'tp-ep', 'replicated', 'split'],
    'simple.quantization' => ['bf16', 'fp8', 'nvfp4', 'mxfp4', 'int4', 'awq', 'gptq', 'autoround'],
    'simple.memory_strategy' => ['balanced', 'max-context', 'max-quality', 'max-throughput', 'max-model-size'],
    'distributed_backend' => ['auto', 'native', 'mp', 'ray'],
    'engine' => ['vllm', 'sglang'],
    'simple.context_length' => ['auto'],
];
// exactly one more segment after the prefix, starting with a letter
const TS_PREFIXED_FIELD = '/^(?:advanced\.extra_vllm_flags\.[a-z][a-z0-9_-]{0,47}|advanced\.compilation_config\.[a-z][a-z0-9_]{0,47})$/';
const TS_VERSION = '/^\d{1,3}(?:\.\d{1,4}){1,2}$/';
const TS_MAX_BODY = 16384;

function ts_config(): array
{
    static $config = null;
    if ($config !== null) {
        return $config;
    }
    // never inside the web folder: the environment, or next to the folder the domain points to
    $candidates = array_filter([
        getenv('TWINSPARK_CONFIG') ?: null,
        dirname(__DIR__, 2) . '/twinspark-private/config.php',
    ]);
    foreach ($candidates as $path) {
        if (is_file($path)) {
            $config = require $path;
            if (is_array($config) && is_string($config['secret'] ?? null) && strlen($config['secret']) >= 32) {
                $config['_dir'] = dirname($path);
                return $config;
            }
            ts_fail(500, 'the server configuration is incomplete');
        }
    }
    ts_fail(500, 'the server is not configured yet');
}

function ts_db(): PDO
{
    static $pdo = null;
    if ($pdo !== null) {
        return $pdo;
    }
    $c = ts_config();
    $dsn = $c['dsn'] ?? sprintf('mysql:host=%s;dbname=%s;charset=utf8mb4', $c['db_host'] ?? '', $c['db_name'] ?? '');
    try {
        $pdo = new PDO($dsn, $c['db_user'] ?? null, $c['db_pass'] ?? null, [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES => false,
        ]);
    } catch (PDOException $e) {
        error_log('twinspark: database connection failed: ' . $e->getMessage());
        ts_fail(503, 'the database is not reachable right now');
    }
    return $pdo;
}

/** A keyed hash: the stored value cannot be turned back into the address or the reporter id. */
function ts_hash(string $value): string
{
    return hash_hmac('sha256', $value, ts_config()['secret']);
}

/** The address a rate limit counts: an IPv6 address by its /64 (one connection usually has a whole /64). */
function ts_client_bucket(string $addr): string
{
    $bin = @inet_pton($addr);
    if ($bin !== false && strlen($bin) === 16) {
        return inet_ntop(substr($bin, 0, 8) . str_repeat("\0", 8)) . '/64';
    }
    return $addr;
}

/** The cached full listing of stats.php, in the private folder; report.php removes it on every report. */
function ts_stats_cache(): string
{
    return ts_config()['_dir'] . '/stats-cache.json';
}

function ts_json(int $status, array $body): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('X-Content-Type-Options: nosniff');
    header('Cache-Control: no-store');
    echo json_encode($body, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

function ts_fail(int $status, string $message): never
{
    ts_json($status, ['ok' => false, 'error' => $message]);
}

/** An IPv4 or IPv6 address anywhere in the text (also compressed forms like fe80::1). */
function ts_address(string $v): bool
{
    if (preg_match('/(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\d)/', $v) === 1) {
        return true;
    }
    foreach (preg_split('/[\s,;()\[\]<>"\'`=\/|]+/', $v) ?: [] as $tok) {
        $tok = trim($tok, '.');
        if (substr_count($tok, ':') >= 2
            && filter_var(explode('%', $tok, 2)[0], FILTER_VALIDATE_IP, FILTER_FLAG_IPV6) !== false) {
            return true;
        }
    }
    return false;
}

function ts_word($v): bool
{
    return is_string($v) && preg_match('/^[A-Za-z0-9_.:+-]{1,48}$/', $v) === 1 && !ts_address($v);
}

function ts_plain($v): bool
{
    if ($v === null || is_bool($v)) {
        return true;
    }
    if (is_int($v) || is_float($v)) {
        return is_finite((float)$v) && abs($v) < 1e12;
    }
    return ts_word($v);
}

/**
 * A named tuning field takes a number, a boolean, a word from its fixed list or a short name word (no dots or
 * hyphens); a free-form flag (extra_vllm_flags.<flag>, compilation options) only a number or a boolean.
 */
function ts_reportable($field, $value): bool
{
    if (!is_string($field) || !preg_match('/^[a-z_][a-z0-9_.-]{0,95}$/', $field) || !ts_plain($value)) {
        return false;
    }
    if (in_array($field, TS_TUNING, true)) {
        if (is_string($value)) {
            return isset(TS_ENUMS[$field]) ? in_array($value, TS_ENUMS[$field], true)
                : preg_match('/^[A-Za-z][A-Za-z0-9_]{0,23}$/', $value) === 1;
        }
        return true;
    }
    return preg_match(TS_PREFIXED_FIELD, $field) === 1
        && ($value === null || is_bool($value) || is_int($value) || is_float($value));
}

/** The report, checked field by field; throws InvalidArgumentException with the reason. */
function ts_check_report($doc): array
{
    $bad = static function (string $why): never {
        throw new InvalidArgumentException($why);
    };
    $str = static fn($v): string => is_string($v) ? $v : '';
    if (!is_array($doc) || ($doc['format'] ?? null) !== TS_FORMAT) {
        $bad('not a ' . TS_FORMAT . ' document');
    }
    $allowed = ['format', 'recipe', 'recipe_sha256', 'mode', 'outcome', 'failure_stage', 'twinspark', 'engine',
        'engine_version', 'topology', 'hardware', 'benchmarks', 'changes', 'comment', 'reporter'];
    $extra = array_diff(array_keys($doc), $allowed);
    if ($extra) {
        $bad('unexpected fields: ' . implode(', ', array_map('strval', array_slice($extra, 0, 5))));
    }
    if (!preg_match('/^[a-z0-9][a-z0-9._-]{0,80}$/', $str($doc['recipe'] ?? null))
        || !preg_match('/^[0-9a-f]{64}$/', $str($doc['recipe_sha256'] ?? null))) {
        $bad('recipe and recipe_sha256 are required');
    }
    $mode = $doc['mode'] ?? null;
    if ($mode !== null && !preg_match('/^[a-z][a-z0-9-]{0,30}$/', $str($mode))) {
        $bad('invalid mode');
    }
    if (!in_array($doc['outcome'] ?? null, TS_OUTCOMES, true)) {
        $bad('outcome must be one of ' . implode(', ', TS_OUTCOMES));
    }
    $stage = $doc['failure_stage'] ?? null;
    if ($stage !== null && !in_array($stage, TS_STAGES, true)) {
        $bad('invalid failure_stage');
    }
    foreach (['twinspark', 'engine_version'] as $k) {
        $v = $doc[$k] ?? null;
        if ($v !== null && !preg_match(TS_VERSION, $str($v))) {
            $bad("invalid $k");
        }
    }
    if (!isset($doc['twinspark'])) {
        $bad('twinspark version is required');
    }
    if (!in_array($doc['engine'] ?? null, TS_ENUMS['engine'], true)) {
        $bad('invalid engine');
    }
    if (isset($doc['topology']) && !in_array($doc['topology'], TS_ENUMS['simple.topology'], true)) {
        $bad('invalid topology');
    }
    if (($doc['hardware'] ?? null) !== '2x DGX Spark') {
        $bad('invalid hardware');
    }
    $benches = $doc['benchmarks'] ?? null;
    if (!is_array($benches) || count($benches) > 8 || (array_is_list($benches) && $benches !== [])) {
        $bad('invalid benchmarks');
    }
    foreach ($benches as $preset => $nums) {
        if (!in_array($preset, TS_PRESETS, true) || !is_array($nums) || array_diff(array_keys($nums), TS_BENCH_FIELDS)) {
            $bad('invalid benchmark numbers');
        }
        foreach ($nums as $v) {
            if (!(is_int($v) || is_float($v) || is_bool($v)) || !ts_plain($v)) {
                $bad('invalid benchmark numbers');
            }
        }
    }
    $changes = $doc['changes'] ?? null;
    if (!is_array($changes) || !array_is_list($changes) || count($changes) > 40) {
        $bad('invalid changes');
    }
    foreach ($changes as $c) {
        if (!is_array($c) || count($c) !== 2 || !array_key_exists('field', $c) || !array_key_exists('value', $c)) {
            $bad('a changed setting is malformed');
        }
        if (!ts_reportable($c['field'], $c['value'])) {
            $bad('a changed setting is not reportable');
        }
    }
    $comment = $doc['comment'] ?? null;
    if (!is_string($comment) || mb_strlen($comment) > 500) {
        $bad('comment: text up to 500 characters');
    }
    if (ts_address($comment) || preg_match('#(?:^|\s)(?:/home/|/Users/|~/|[A-Za-z]:\\\\)#', $comment)) {
        $bad('the comment contains an address or a path');
    }
    $reporter = $doc['reporter'] ?? null;
    if ($reporter !== null && !preg_match('/^[0-9a-f]{32}$/', $str($reporter))) {
        $bad('invalid reporter id');
    }
    return $doc;
}
