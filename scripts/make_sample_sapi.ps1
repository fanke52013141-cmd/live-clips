# Synthesize Chinese sample with local Windows SAPI (Microsoft Huihui Desktop, zh-CN).
# Pure ASCII script; Chinese text is read from a UTF-8 file to avoid GBK issues.
# Output: 16kHz 16bit mono WAV (Paraformer native format). Fully offline.
$ErrorActionPreference = "Stop"
$text = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot "sample_text_zh.txt")
$outDir = Join-Path $PSScriptRoot "..\.data-dev\acceptance"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$out = Join-Path $outDir "sample_zh.wav"
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice("Microsoft Huihui Desktop")
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($out, $fmt)
$s.Speak($text) | Out-Null
$s.Dispose()
Write-Output ("WAV_OK " + $out)