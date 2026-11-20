param([Parameter(Mandatory)][string]$ProjectRoot,[Parameter(Mandatory)][string]$PromptFile,
      [Parameter(Mandatory)][string]$OutputDirectory,[Parameter(Mandatory)][int]$Turn,[ValidateSet('demo_company_action.schema.json','demo_company_chat.schema.json','demo_company_command_intake.schema.json','demo_company_conversation.schema.json')][string]$SchemaFile='demo_company_action.schema.json')
$ErrorActionPreference='Stop'
$root=[IO.Path]::GetFullPath($ProjectRoot)
$boundary=(Join-Path $root 'ia/.runtime').TrimEnd('\')+'\'
foreach($path in @($PromptFile,$OutputDirectory)){
    $resolved=[IO.Path]::GetFullPath($path)
    if(-not $resolved.StartsWith($boundary,[StringComparison]::OrdinalIgnoreCase)){throw 'Model transport path outside runtime'}
}
. ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $root 'ia/src/CodexModel.ps1'),[Text.Encoding]::UTF8)))
$prompt=[IO.File]::ReadAllText($PromptFile,[Text.Encoding]::UTF8)
$null=Invoke-IACodexModel -ProjectRoot $root -Prompt $prompt -OutputDirectory $OutputDirectory -Turn $Turn -SchemaFile $SchemaFile
