# SAPI offline TTS (Windows). ASCII-only script; Chinese text comes from a UTF-8 file.
# Usage: powershell -File sapi_tts.ps1 -TextFile <utf8.txt> -OutWav <out.wav>
param(
    [Parameter(Mandatory=$true)][string]$TextFile,
    [Parameter(Mandatory=$true)][string]$OutWav
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like "zh*" } | Select-Object -First 1
if ($voice) { $synth.SelectVoice($voice.VoiceInfo.Name) }
$synth.SetOutputToWaveFile($OutWav)
$synth.Speak($text)
$synth.Dispose()
if (-not (Test-Path $OutWav)) { throw "SAPI produced no output" }
Write-Output "SAPI_OK"