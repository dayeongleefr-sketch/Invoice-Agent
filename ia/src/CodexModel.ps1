# Model transport only. Business tools are dispatched by the host, never by a shell command from the model.
Set-StrictMode -Version 2.0
function ConvertTo-IANativeArgument([string]$Value){
    if($Value -notmatch '[\s"]' -and $Value.Length){return $Value}
    return '"'+([regex]::Replace(([regex]::Replace($Value,'(\\*)"','$1$1\"')),'(\\+)$','$1$1'))+'"'
}
function Invoke-IACodexModel {
    param([string]$ProjectRoot,[string]$Prompt,[string]$OutputDirectory,[int]$Turn,[int]$TimeoutSeconds=180,[string]$SchemaFile='agent_action.schema.json')
    $ErrorActionPreference='Stop'
    $command=Get-Command codex.exe -ErrorAction Stop
    $output=Join-Path $OutputDirectory ('model_response_'+$Turn+'.json')
    if($SchemaFile -cnotin @('agent_action.schema.json','demo_company_action.schema.json','demo_company_chat.schema.json','demo_company_command_intake.schema.json','demo_company_conversation.schema.json')){throw 'Unsupported model schema'}
    $schema=Join-Path $ProjectRoot ('ia/config/'+$SchemaFile)
    if(Test-Path -LiteralPath $output){throw 'Model response already exists'}
    $boundary=[IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')+'\'
    $cursor=[IO.Path]::GetFullPath($OutputDirectory)
    if(-not $cursor.StartsWith($boundary,[StringComparison]::OrdinalIgnoreCase)){throw 'Model output outside workspace'}
    while($cursor.StartsWith($boundary,[StringComparison]::OrdinalIgnoreCase)){
        if((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Model output reparse point rejected'}
        $cursor=[IO.Path]::GetDirectoryName($cursor)
    }
    $schemaHash=(Get-FileHash -LiteralPath $schema).Hash
    $arguments=@('exec','--ignore-user-config','--skip-git-repo-check','--ephemeral','--sandbox','read-only','-C',$OutputDirectory,'--config','features.shell_tool=false','--config','web_search="disabled"','--config','project_doc_max_bytes=0','--output-schema',$schema,'--output-last-message',$output,'--json','-')
    $start=New-Object Diagnostics.ProcessStartInfo
    $start.FileName=$command.Source
    $start.Arguments=($arguments|ForEach-Object {ConvertTo-IANativeArgument $_}) -join ' '
    $start.UseShellExecute=$false;$start.CreateNoWindow=$true
    $start.RedirectStandardInput=$true;$start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true
    $start.StandardOutputEncoding=[Text.Encoding]::UTF8;$start.StandardErrorEncoding=[Text.Encoding]::UTF8
    # Use the existing authentication location; never copy or inspect credential contents.
    if([string]::IsNullOrWhiteSpace($env:CODEX_HOME)){
        $profileDirectory=[Environment]::GetEnvironmentVariable('USERPROFILE')
        if([string]::IsNullOrWhiteSpace($profileDirectory)){throw 'AI authentication directory unavailable'}
        $start.EnvironmentVariables['CODEX_HOME']=Join-Path $profileDirectory '.codex'
    }
    $process=[Diagnostics.Process]::Start($start)
    try {
        $stdoutTask=$process.StandardOutput.ReadToEndAsync();$stderrTask=$process.StandardError.ReadToEndAsync()
        $promptBytes=[Text.Encoding]::UTF8.GetBytes($Prompt)
        $writeTask=$process.StandardInput.BaseStream.WriteAsync($promptBytes,0,$promptBytes.Length)
        if(-not $writeTask.Wait(10000)){$process.Kill();[void]$process.WaitForExit(5000);throw 'AI prompt transport timed out'}
        $process.StandardInput.BaseStream.Flush();$process.StandardInput.Close()
        if(-not $process.WaitForExit($TimeoutSeconds*1000)){$process.Kill();[void]$process.WaitForExit(5000);throw 'AI model response timed out'}
        $stdout=$stdoutTask.Result;$stderr=$stderrTask.Result
        # Store only selected event metadata, not raw stderr, account details, or credentials.
        $events=@(foreach($line in ($stdout -split "`n")){
            try {$event=$line|ConvertFrom-Json -ErrorAction Stop
                if($event.type -eq 'turn.completed'){[pscustomobject]@{type=$event.type;usage=$event.usage}}
                if($event.type -eq 'item.completed' -and $event.item.type -ne 'agent_message'){[pscustomobject]@{type=$event.type;item_type=$event.item.type}}
            } catch {}
        })
        [IO.File]::WriteAllText((Join-Path $OutputDirectory ('model_events_'+$Turn+'.json')),(ConvertTo-Json -InputObject $events -Depth 8),(New-Object Text.UTF8Encoding($false)))
        if($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $output)){
            $reason='provider_error'
            if($stderr -match 'Could not find home directory'){$reason='home_directory_unavailable'}
            if($stderr -match 'unexpected argument|unrecognized|invalid value'){$reason='invalid_cli_argument'}
            if($stderr -match 'permission denied|Access is denied|os error 5'){$reason='filesystem_permission_denied'}
            if($stderr -match '401|unauthorized|not logged in'){$reason='authentication_required'}
            if($stderr -match 'connect|resolve host|network|stream disconnected'){$reason='connection_failure'}
            if($stderr -match 'invalid.*schema|schema.*invalid'){$reason='invalid_schema'}
            throw ('AI provider execution failed (exit '+$process.ExitCode+', '+$reason+'); no model result was fabricated')
        }
        if(@($events|Where-Object type -EQ 'turn.completed').Count -ne 1){throw 'AI provider did not report a completed turn'}
        if((Get-FileHash -LiteralPath $schema).Hash -ne $schemaHash){throw 'AI response schema changed'}
        if(@($events|Where-Object { $_.PSObject.Properties['item_type'] -and $_.item_type -notin @('reasoning') }).Count){throw 'Unexpected model-side tool use'}
        $value=[IO.File]::ReadAllText($output,[Text.Encoding]::UTF8)|ConvertFrom-Json
        return $value
    } finally {$process.Dispose()}
}
