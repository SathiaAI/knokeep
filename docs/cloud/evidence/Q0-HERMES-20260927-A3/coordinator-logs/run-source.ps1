$B='<A3_ROOT>'
$brief=Get-Content '<PRIVATE_DISPATCH_DIR>' -Raw
$brief=$brief -replace 'Q0-HERMES-20260927-A1','Q0-HERMES-20260927-A3' -replace 'q0/hermes/20260927-a1','q0/hermes/20260927-a3' -replace 'attempt generation: 1','attempt generation: 3'
[IO.File]::WriteAllText("$B\evidence\a3-prompt.md", $brief + (Get-Content "$B\evidence\a3-addendum.md" -Raw))
$env:HERMES_HOME="$B\home3"; $env:HERMES_DISABLE_LAZY_INSTALLS='1'
$env:HERMES_A3_LOCAL_KEY=(Get-Content 'C:\Users\<user>\AppData\Local\hermes\runtimes\llamacpp\.api_key' -Raw).Trim()
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$t0=Get-Date; "START $($t0.ToUniversalTime().ToString('o'))" | Set-Content "$B\evidence\10-source.meta.txt"
$p=Start-Process -FilePath '<HERMES_VENV>\Scripts\hermes.exe' -ArgumentList @('chat','--query-file',"$B\evidence\a3-prompt.md",'--oneshot','--format','stream-json','--provider','custom','-m','GLM-4.7-Flash-Q4_K_M','-t','terminal,file','--max-turns','20','--run-budget','1140','--in',"$B\repo") -NoNewWindow -PassThru -RedirectStandardOutput "$B\evidence\10-source.stdout.jsonl" -RedirectStandardError "$B\evidence\10-source.stderr.txt"
if(-not $p.WaitForExit(1200000)){ $p.Kill(); 'KILLED at 1200s' | Add-Content "$B\evidence\10-source.meta.txt" }
"EXIT $($p.ExitCode) END $((Get-Date).ToUniversalTime().ToString('o')) elapsed_s $([int]((Get-Date)-$t0).TotalSeconds)" | Add-Content "$B\evidence\10-source.meta.txt"
