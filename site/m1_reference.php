<?php
/**
 * model_1 — reference implementation of the job queue + worker endpoints.
 *
 * Tested with PHP 8.3. Adapt into kie-proxy.php: keep the protocol (actions,
 * header, JSON shapes) exactly as is; storage paths, task records, credits and
 * gallery saving should use the site's existing helpers.
 *
 * Worker actions (called by the external worker, NOT by the browser):
 *   POST ?action=m1_next      header X-Worker-Key   -> {"job": {...}} | {"job": null}
 *   POST ?action=m1_complete  header X-Worker-Key   multipart: job_id, files[], resolutions ("2k,2k,1k")
 *   POST ?action=m1_fail      header X-Worker-Key   JSON {job_id, error_code}
 */

const M1_ASPECTS      = ['16:9', '4:3', '1:1', '3:4', '9:16'];
const M1_STALE_SEC    = 1200;  // claimed but not finished -> requeue once, then fail (2K x4 can take ~10 min)
const M1_EXPIRE_SEC   = 2700;  // job older than this and not done -> fail
const M1_ONLINE_SEC   = 90;    // worker considered online if polled within this window
const M1_KEEP_SEC     = 7 * 86400; // finished jobs kept in the queue file
const M1_MAX_FILE     = 20 * 1024 * 1024;
const M1_MAX_PROMPT   = 4000;
const M1_ERROR_CODES  = ['timeout', 'generation_failed', 'content_blocked', 'quota', 'worker_auth', 'bad_request', 'refs_unsupported', 'upscale_unavailable'];
const M1_MAX_REFS     = 10;

/** Paths: change to the site's data/ dir (already closed to the web by .htaccess). */
function m1_queue_path(): string  { return M1_DATA_DIR . '/m1_jobs.json'; }
function m1_worker_path(): string { return M1_DATA_DIR . '/m1_worker.json'; }

/** Run $fn(array &$jobs) under an exclusive lock and persist the result. */
function m1_with_queue(callable $fn)
{
    $fh = fopen(m1_queue_path(), 'c+');
    if (!$fh) { throw new RuntimeException('m1 queue not writable'); }
    try {
        flock($fh, LOCK_EX);
        $raw  = stream_get_contents($fh);
        $jobs = $raw ? (json_decode($raw, true) ?: []) : [];
        $result = $fn($jobs);
        ftruncate($fh, 0);
        rewind($fh);
        fwrite($fh, json_encode($jobs, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
        fflush($fh);
        return $result;
    } finally {
        flock($fh, LOCK_UN);
        fclose($fh);
    }
}

/** Expire / requeue stale jobs and prune old finished ones. Call inside m1_with_queue. */
function m1_housekeeping(array &$jobs, ?callable $on_fail = null): void
{
    $now = time();
    foreach ($jobs as $id => &$job) {
        $open = in_array($job['status'], ['queued', 'processing'], true);
        if ($open && $now - $job['created_at'] > M1_EXPIRE_SEC) {
            $job['status'] = 'failed';
            $job['error_code'] = 'timeout';
            $job['finished_at'] = $now;
            if ($on_fail) { $on_fail($job); }
            continue;
        }
        if ($job['status'] === 'processing' && $now - ($job['claimed_at'] ?? $now) > M1_STALE_SEC) {
            if (($job['attempts'] ?? 0) < 2) {
                $job['status'] = 'queued';           // worker restarted or lost the job: retry
            } else {
                $job['status'] = 'failed';
                $job['error_code'] = 'timeout';
                $job['finished_at'] = $now;
                if ($on_fail) { $on_fail($job); }
            }
        }
        if (!$open && $now - ($job['finished_at'] ?? $job['created_at']) > M1_KEEP_SEC) {
            unset($jobs[$id]);
        }
    }
    unset($job);
}

function m1_worker_online(): bool
{
    $seen = @json_decode(@file_get_contents(m1_worker_path()), true)['last_seen'] ?? 0;
    return time() - (int)$seen <= M1_ONLINE_SEC;
}

/** Site side: put a job in the queue (called from action=create, family "model_1"). */
function m1_enqueue(string $task_id, string $prompt, string $aspect, int $count, string $owner, array $image_urls = [], string $resolution = '1k'): array
{
    if (!in_array($resolution, ['1k', '2k'], true)) { throw new InvalidArgumentException('bad resolution'); }
    // Reference images: only this site's own gallery files (paths like /gallery/RefAssets/x.jpg).
    $refs = [];
    foreach (array_slice(array_values($image_urls), 0, M1_MAX_REFS) as $url) {
        $path = parse_url((string)$url, PHP_URL_PATH);
        if (!is_string($path) || !preg_match('#^/gallery/[A-Za-z0-9_./-]+\.(jpe?g|png|webp)$#i', $path) || strpos($path, '..') !== false) {
            throw new InvalidArgumentException('bad image_urls');
        }
        $refs[] = $path;
    }
    if (count($image_urls) > M1_MAX_REFS) { throw new InvalidArgumentException('too many image_urls'); }
    $prompt = trim($prompt);
    if ($prompt === '' || mb_strlen($prompt) > M1_MAX_PROMPT) { throw new InvalidArgumentException('bad prompt'); }
    if (!in_array($aspect, M1_ASPECTS, true)) { throw new InvalidArgumentException('bad aspect_ratio'); }
    if (!preg_match('/^[A-Za-z0-9_-]{6,64}$/', $task_id)) { throw new InvalidArgumentException('bad task id'); }
    $job = [
        'id' => $task_id, 'owner' => $owner, 'prompt' => $prompt, 'aspect_ratio' => $aspect,
        'count' => max(1, min(4, $count)), 'status' => 'queued', 'attempts' => 0,
        'created_at' => time(), 'claimed_at' => null, 'finished_at' => null,
        'urls' => [], 'error_code' => null, 'image_urls' => $refs, 'type' => 'generate', 'resolution' => $resolution,
    ];
    m1_with_queue(function (array &$jobs) use ($job) { $jobs[$job['id']] = $job; });
    return $job;
}

/**
 * Site side: 2K version of one finished model_1 image (called from action=m1_upscale).
 * $index = 0-based position of the image in the source task's urls.
 * Returns ['job' => array, 'cached' => bool]: if a finished 2K job already exists its
 * urls are returned as is; a queued/processing one is reused; a failed one is retried.
 */
function m1_enqueue_upscale(string $source_id, int $index, string $owner): array
{
    if (!preg_match('/^[A-Za-z0-9_-]{6,56}$/', $source_id)) { throw new InvalidArgumentException('bad task id'); }
    return m1_with_queue(function (array &$jobs) use ($source_id, $index, $owner) {
        m1_housekeeping($jobs);
        $src = $jobs[$source_id] ?? null;
        if (!$src || ($src['type'] ?? 'generate') !== 'generate' || $src['status'] !== 'done') {
            throw new InvalidArgumentException('source not finished');
        }
        if ($src['owner'] !== $owner) { throw new InvalidArgumentException('not owner'); }
        if ($index < 0 || $index >= count($src['urls'])) { throw new InvalidArgumentException('bad index'); }
        $id = $source_id . '_2k' . $index;
        $old = $jobs[$id] ?? null;
        if ($old && in_array($old['status'], ['done', 'queued', 'processing'], true)) {
            return ['job' => $old, 'cached' => $old['status'] === 'done'];
        }
        $job = [
            'id' => $id, 'owner' => $owner, 'type' => 'upscale', 'source_id' => $source_id,
            'index' => $index, 'scale' => '2k', 'prompt' => '', 'aspect_ratio' => $src['aspect_ratio'],
            'count' => 1, 'status' => 'queued', 'attempts' => 0, 'created_at' => time(),
            'claimed_at' => null, 'finished_at' => null, 'urls' => [], 'error_code' => null, 'image_urls' => [],
        ];
        $jobs[$id] = $job;
        return ['job' => $job, 'cached' => false];
    });
}

/** Site side: job state for action=status. */
function m1_job(string $task_id): ?array
{
    return m1_with_queue(function (array &$jobs) use ($task_id) {
        m1_housekeeping($jobs);
        return $jobs[$task_id] ?? null;
    });
}

/** Position in queue (0 = being processed / next). */
function m1_position(string $task_id): int
{
    return m1_with_queue(function (array &$jobs) use ($task_id) {
        $pos = 0;
        foreach ($jobs as $id => $job) {
            if ($id === $task_id) { return $pos; }
            if ($job['status'] === 'queued' || $job['status'] === 'processing') { $pos++; }
        }
        return $pos;
    });
}

/* ------------------------------------------------------------------ worker side */

function m1_json($data, int $code = 200): void
{
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function m1_auth_worker(string $expected_key): void
{
    $given = $_SERVER['HTTP_X_WORKER_KEY'] ?? '';
    if ($expected_key === '' || !is_string($given) || !hash_equals($expected_key, $given)) {
        m1_json(['error' => 'forbidden'], 403);
    }
}

/** action=m1_next: heartbeat + hand out the oldest queued job. */
function m1_handle_next(?callable $on_fail = null): void
{
    @file_put_contents(m1_worker_path(), json_encode(['last_seen' => time()]), LOCK_EX);
    $job = m1_with_queue(function (array &$jobs) use ($on_fail) {
        m1_housekeeping($jobs, $on_fail);
        $pick = null;
        foreach ($jobs as $id => $job) {
            if ($job['status'] === 'queued' && ($pick === null || $job['created_at'] < $jobs[$pick]['created_at'])) {
                $pick = $id;
            }
        }
        if ($pick === null) { return null; }
        $jobs[$pick]['status'] = 'processing';
        $jobs[$pick]['claimed_at'] = time();
        $jobs[$pick]['attempts'] = ($jobs[$pick]['attempts'] ?? 0) + 1;
        $j = $jobs[$pick];
        if (($j['type'] ?? 'generate') === 'upscale') {
            return ['id' => $j['id'], 'type' => 'upscale', 'source_id' => $j['source_id'],
                    'index' => $j['index'], 'scale' => $j['scale']];
        }
        return ['id' => $j['id'], 'type' => 'generate', 'prompt' => $j['prompt'], 'aspect_ratio' => $j['aspect_ratio'],
                'count' => $j['count'], 'image_urls' => $j['image_urls'] ?? [],
                'resolution' => $j['resolution'] ?? '1k'];
    });
    m1_json(['job' => $job]);
}

/**
 * action=m1_complete: accept uploaded images.
 * $save = function(string $tmp_path, string $ext, array $job, int $index): string  -> public URL
 * (use the site's gallery_save naming: gallery/PicAssets/<date>-<time>_<taskId>[_index].<ext>)
 */
function m1_handle_complete(callable $save, ?callable $on_done = null): void
{
    $job_id = (string)($_POST['job_id'] ?? '');
    if (!preg_match('/^[A-Za-z0-9_-]{6,64}$/', $job_id)) { m1_json(['error' => 'bad job_id'], 400); }

    $files = $_FILES['files'] ?? null;
    if (!$files || !is_array($files['tmp_name'])) { m1_json(['error' => 'no files'], 400); }

    $finfo = new finfo(FILEINFO_MIME_TYPE);
    $allowed = ['image/jpeg' => 'jpg', 'image/png' => 'png', 'image/webp' => 'webp'];
    $uploads = [];
    foreach ($files['tmp_name'] as $i => $tmp) {
        if (($files['error'][$i] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK || !is_uploaded_file($tmp)) {
            m1_json(['error' => 'upload failed'], 400);
        }
        if (filesize($tmp) > M1_MAX_FILE) { m1_json(['error' => 'file too large'], 413); }
        $mime = $finfo->file($tmp);
        if (!isset($allowed[$mime]) || @getimagesize($tmp) === false) { m1_json(['error' => 'not an image'], 415); }
        $uploads[] = [$tmp, $allowed[$mime]];
    }

    $result = m1_with_queue(function (array &$jobs) use ($job_id, $uploads, $save, $on_done) {
        if (!isset($jobs[$job_id])) { return ['error' => 'unknown job', 'code' => 404]; }
        $job = &$jobs[$job_id];
        if (!in_array($job['status'], ['processing', 'queued'], true)) {
            return ['error' => 'job already ' . $job['status'], 'code' => 409];
        }
        $urls = [];
        foreach (array_slice($uploads, 0, $job['count']) as $index => [$tmp, $ext]) {
            $urls[] = $save($tmp, $ext, $job, $index);
        }
        $job['status'] = 'done';
        $job['urls'] = $urls;
        // per-file real resolution; '1k' where a 2K upscale failed and the original was sent
        $job['resolutions'] = array_slice(array_filter(explode(',', (string)($_POST['resolutions'] ?? ''))), 0, count($urls));
        $job['finished_at'] = time();
        if ($on_done) { $on_done($job); }
        return ['ok' => true, 'urls' => $urls];
    });
    m1_json($result, $result['code'] ?? 200);
}

/** action=m1_fail: worker could not produce the image. $on_fail($job) should refund credits. */
function m1_handle_fail(?callable $on_fail = null): void
{
    $body = json_decode(file_get_contents('php://input'), true) ?: [];
    $job_id = (string)($body['job_id'] ?? '');
    $code = in_array($body['error_code'] ?? '', M1_ERROR_CODES, true) ? $body['error_code'] : 'generation_failed';
    $result = m1_with_queue(function (array &$jobs) use ($job_id, $code, $on_fail) {
        if (!isset($jobs[$job_id])) { return ['error' => 'unknown job', 'code' => 404]; }
        $job = &$jobs[$job_id];
        if (in_array($job['status'], ['done', 'failed'], true)) { return ['ok' => true]; }
        $job['status'] = 'failed';
        $job['error_code'] = $code;
        $job['finished_at'] = time();
        if ($on_fail) { $on_fail($job); }
        return ['ok' => true];
    });
    m1_json($result, $result['code'] ?? 200);
}
