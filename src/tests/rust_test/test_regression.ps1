# Regression test: rust-migrate-engine critical paths
# Covers P1/P2: copy / mirror+purge / cancel / resume / nested validation
$ErrorActionPreference = 'Stop'
$engine = 'G:\AI\TRAE SOLO CN\trae_xiangmu\xiangmu1\D盘源码\bin\rust-migrate-engine.exe'
$base = Join-Path $env:TEMP 'migrate_test'
Remove-Item $base -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $base | Out-Null
$src = Join-Path $base 'src'
$dst = Join-Path $base 'dst'
New-Item -ItemType Directory -Path $src | Out-Null
New-Item -ItemType Directory -Path (Join-Path $src 'sub') | Out-Null
Set-Content -Path (Join-Path $src 'a.txt') -Value 'hello'
Set-Content -Path (Join-Path $src 'sub\b.txt') -Value 'world'
$big = Join-Path $src 'big.bin'
$fs = [IO.File]::Create($big)
$buf = New-Object byte[] 1048576
for ($i = 0; $i -lt 70; $i++) { $fs.Write($buf, 0, $buf.Length) }
$fs.Close()
$bigLen = (Get-Item $big).Length
Write-Host ("SRC ready: {0} files, big.bin={1} bytes" -f (Get-ChildItem $src -Recurse -File).Count, $bigLen)

# === 1. copy mode ===
$job = @{
    source = $src; target = $dst; mode = 'copy'
    retry = @{ max_attempts = 1; backoff_base_ms = 0; network_path = $false }
    flush_checkpoint_mb = 64
    purge = @{ enabled = $false; soft_delete = $true; dry_run = $false }
    background_mode = $false; write_through = $false; large_file_threshold_mb = 1
} | ConvertTo-Json -Depth 5
$jobPath = Join-Path $base 'job_copy.json'
Set-Content -Path $jobPath -Value $job -Encoding UTF8
$out = & $engine --job $jobPath --log-format jsonl 2>&1
$rc = $LASTEXITCODE
Write-Host ("=== 1. copy rc={0} ===" -f $rc)
$out | Select-Object -First 5
$dstBig = Join-Path $dst 'big.bin'
if ((Get-Item $dstBig).Length -ne $bigLen) { Write-Host 'FAIL big.bin length mismatch'; exit 1 }
if (-not (Test-Path (Join-Path $dst 'sub\b.txt'))) { Write-Host 'FAIL sub\b.txt missing'; exit 1 }
$ckptFile = "$dstBig.migrate-ckpt"
if (Test-Path $ckptFile) { Write-Host 'FAIL ckpt should be removed after completion'; exit 1 }
Write-Host 'PASS copy mode (with large file)'

# === 2. mirror + purge dry-run (no real delete) ===
$dst2 = Join-Path $base 'dst2'
New-Item -ItemType Directory -Path $dst2 | Out-Null
Set-Content -Path (Join-Path $dst2 'stale.txt') -Value 'should be purged'
$job = @{
    source = $src; target = $dst2; mode = 'mirror'
    retry = @{ max_attempts = 1; backoff_base_ms = 0; network_path = $false }
    flush_checkpoint_mb = 64
    purge = @{ enabled = $true; soft_delete = $true; dry_run = $true }
    background_mode = $false; write_through = $false; large_file_threshold_mb = 1
} | ConvertTo-Json -Depth 5
$jobPath = Join-Path $base 'job_mirror.json'
Set-Content -Path $jobPath -Value $job -Encoding UTF8
$out = & $engine --job $jobPath --log-format jsonl 2>&1
$rc = $LASTEXITCODE
Write-Host ("=== 2. mirror+dry-run rc={0} ===" -f $rc)
$purgeEvt = $out | Where-Object { $_ -match '"event":"purge"' }
Write-Host ("purge events: {0}" -f ($purgeEvt | Measure-Object).Count)
if (-not (Test-Path (Join-Path $dst2 'stale.txt'))) { Write-Host 'FAIL dry-run should not delete stale.txt'; exit 1 }
Write-Host 'PASS mirror dry-run no real delete'

# === 3. mirror + purge soft-delete (real delete to recycle bin) ===
$dst3 = Join-Path $base 'dst3'
New-Item -ItemType Directory -Path $dst3 | Out-Null
Set-Content -Path (Join-Path $dst3 'stale2.txt') -Value 'should be soft-deleted'
$job = @{
    source = $src; target = $dst3; mode = 'mirror'
    retry = @{ max_attempts = 1; backoff_base_ms = 0; network_path = $false }
    flush_checkpoint_mb = 64
    purge = @{ enabled = $true; soft_delete = $true; dry_run = $false }
    background_mode = $false; write_through = $false; large_file_threshold_mb = 1
} | ConvertTo-Json -Depth 5
$jobPath = Join-Path $base 'job_mirror_soft.json'
Set-Content -Path $jobPath -Value $job -Encoding UTF8
$out = & $engine --job $jobPath --log-format jsonl 2>&1
$rc = $LASTEXITCODE
Write-Host ("=== 3. mirror+soft-delete rc={0} ===" -f $rc)
if (Test-Path (Join-Path $dst3 'stale2.txt')) { Write-Host 'FAIL stale2.txt should be soft-deleted'; exit 1 }
Write-Host 'PASS mirror soft-delete to recycle bin'

# === 4. nested path validation (should be rejected) ===
$nested = Join-Path $src 'nested'
New-Item -ItemType Directory -Path $nested | Out-Null
$job = @{
    source = $src; target = $nested; mode = 'mirror'
    retry = @{ max_attempts = 1; backoff_base_ms = 0; network_path = $false }
    flush_checkpoint_mb = 64
    purge = @{ enabled = $true; soft_delete = $true; dry_run = $false }
    background_mode = $false; write_through = $false; large_file_threshold_mb = 1
} | ConvertTo-Json -Depth 5
$jobPath = Join-Path $base 'job_nested.json'
Set-Content -Path $jobPath -Value $job -Encoding UTF8
$out = & $engine --job $jobPath --log-format jsonl 2>&1
$rc = $LASTEXITCODE
Write-Host ("=== 4. nested validation rc={0} (expected 16) ===" -f $rc)
if ($rc -ne 16) { Write-Host 'FAIL nested should be rejected rc=16'; exit 1 }
Write-Host 'PASS nested path rejected'

# === 5. cancel mechanism ===
# 文件需要足够大,确保在 sleep 窗口内引擎仍在写:1.2GB(write_through 关闭走缓存也够慢)
$srcBig = Join-Path $base 'src_big'
$dstBig2 = Join-Path $base 'dst_big'
New-Item -ItemType Directory -Path $srcBig | Out-Null
$big2 = Join-Path $srcBig 'big2.bin'
$fs = [IO.File]::Create($big2)
$buf = New-Object byte[] 1048576
for ($i = 0; $i -lt 1200; $i++) { $fs.Write($buf, 0, $buf.Length) }
$fs.Close()
$cancelToken = Join-Path $base 'cancel.flag'
Remove-Item $cancelToken -Force -ErrorAction SilentlyContinue
$job = @{
    source = $srcBig; target = $dstBig2; mode = 'copy'
    retry = @{ max_attempts = 1; backoff_base_ms = 0; network_path = $false }
    flush_checkpoint_mb = 64
    purge = @{ enabled = $false; soft_delete = $true; dry_run = $false }
    background_mode = $false; write_through = $true; large_file_threshold_mb = 1
    cancel_token = $cancelToken
} | ConvertTo-Json -Depth 5
$jobPath = Join-Path $base 'job_cancel.json'
Set-Content -Path $jobPath -Value $job -Encoding UTF8
$proc = Start-Process -FilePath $engine -ArgumentList @('--job', $jobPath, '--log-format', 'jsonl') -RedirectStandardOutput (Join-Path $base 'cancel.out') -RedirectStandardError (Join-Path $base 'cancel.err') -PassThru -NoNewWindow
Start-Sleep -Milliseconds 300
Set-Content -Path $cancelToken -Value 'cancel'
$proc.WaitForExit(30000) | Out-Null
$rc = $proc.ExitCode
$out = Get-Content (Join-Path $base 'cancel.out')
Write-Host ("=== 5. cancel rc={0} (expected 255) ===" -f $rc)
$cancelledEvt = $out | Where-Object { $_ -match '"event":"cancelled"' }
Write-Host ("cancelled events: {0}" -f ($cancelledEvt | Measure-Object).Count)
if ($rc -ne 255) { Write-Host 'FAIL cancel should return rc=255 (-1)'; exit 1 }
if (($cancelledEvt | Measure-Object).Count -lt 1) { Write-Host 'FAIL missing cancelled event'; exit 1 }
Write-Host 'PASS cancel mechanism'

# === 6. resume after cancel ===
$dstBig2File = Join-Path $dstBig2 'big2.bin'
$partialLen = (Get-Item $dstBig2File).Length
$ckptAfterCancel = "$dstBig2File.migrate-ckpt"
Write-Host ("After cancel: partial={0}, ckpt_exists={1}" -f $partialLen, (Test-Path $ckptAfterCancel))
$job = @{
    source = $srcBig; target = $dstBig2; mode = 'copy'
    retry = @{ max_attempts = 1; backoff_base_ms = 0; network_path = $false }
    flush_checkpoint_mb = 64
    purge = @{ enabled = $false; soft_delete = $true; dry_run = $false }
    background_mode = $false; write_through = $false; large_file_threshold_mb = 1
} | ConvertTo-Json -Depth 5
$jobPath = Join-Path $base 'job_resume.json'
Set-Content -Path $jobPath -Value $job -Encoding UTF8
$out = & $engine --job $jobPath --log-format jsonl 2>&1
$rc = $LASTEXITCODE
Write-Host ("=== 6. resume rc={0} ===" -f $rc)
$resumeEvt = $out | Where-Object { $_ -match '"key":"resume"' }
Write-Host ("resume events: {0}" -f $resumeEvt)
$finalLen = (Get-Item $dstBig2File).Length
$srcLen = (Get-Item $big2).Length
if ($finalLen -ne $srcLen) { Write-Host ("FAIL resume length mismatch final={0} src={1}" -f $finalLen, $srcLen); exit 1 }
if (Test-Path $ckptAfterCancel) { Write-Host 'FAIL ckpt should be removed after resume completion'; exit 1 }
Write-Host 'PASS resume after cancel'

Write-Host ''
Write-Host '========== ALL TESTS PASSED =========='
Remove-Item $base -Recurse -Force -ErrorAction SilentlyContinue
