param([string]$Tag, [string]$Prompt, [int]$Turns, [int]$Budget, [int]$KillSec)
$B='<A4_ROOT>'
$env:HERMES_HOME="$B\home4"; $env:HERMES_DISABLE_LAZY_INSTALLS='1'
$env:HERMES_A3_LOCAL_KEY=(Get-Content 'C:\Users\<user>\AppData\Local\hermes\runtimes\llamacpp\.api_key' -Raw).Trim()
Remove-Item Env:ANTHROPIC_API_KEY,Env:OPENROUTER_API_KEY,Env:HERMES_PROFILE -ErrorAction SilentlyContinue
$t0=Get-Date; "START $($t0.ToUniversalTime().ToString('o'))" | Set-Content "$B\evidence\$Tag.meta.txt"
$p=Start-Process -FilePath '<HERMES_VENV>\Scripts\hermes.exe' -ArgumentList @('chat','--query-file',$Prompt,'--oneshot','--format','stream-json','--provider','custom','-m','GLM-4.7-Flash-Q4_K_M','-t','terminal,file','--max-turns',"$Turns",'--run-budget',"$Budget",'--in',"$B\repo") -NoNewWindow -PassThru -RedirectStandardOutput "$B\evidence\$Tag.stdout.jsonl" -RedirectStandardError "$B\evidence\$Tag.stderr.txt"
if(-not $p.WaitForExit($KillSec*1000)){ $p.Kill(); "KILLED at $KillSec s" | Add-Content "$B\evidence\$Tag.meta.txt" }
$p.WaitForExit()
"EXIT $($p.ExitCode) END $((Get-Date).ToUniversalTime().ToString('o')) elapsed_s $([int]((Get-Date)-$t0).TotalSeconds)" | Add-Content "$B\evidence\$Tag.meta.txt"
