param([Parameter(Mandatory=$true)][string]$SourcePath,[Parameter(Mandatory=$true)][string]$OutputPath,[int]$MaxPages=100)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]|Out-Null
[Windows.Data.Pdf.PdfDocument,Windows.Data.Pdf,ContentType=WindowsRuntime]|Out-Null
[Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]|Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]|Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]|Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
[Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null
$taskMethod=[System.WindowsRuntimeSystemExtensions].GetMethods()|Where-Object {$_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'}|Select-Object -First 1
$actionMethod=[System.WindowsRuntimeSystemExtensions].GetMethods()|Where-Object {$_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'}|Select-Object -First 1
function Await-Op($Op,[Type]$ResultType){$t=$taskMethod.MakeGenericMethod($ResultType).Invoke($null,@($Op));$t.Wait();return $t.Result}
function Await-Action($Op){$t=$actionMethod.Invoke($null,@($Op));$t.Wait()}
$file=Await-Op ([Windows.Storage.StorageFile]::GetFileFromPathAsync((Resolve-Path -LiteralPath $SourcePath).Path)) ([Windows.Storage.StorageFile])
$pdf=Await-Op ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
if($pdf.PageCount -gt $MaxPages){throw 'contract_pdf_page_limit'}
$ocr=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if($null -eq $ocr){throw 'local_ocr_unavailable'}
$pages=[Collections.Generic.List[object]]::new()
for($i=0;$i -lt $pdf.PageCount;$i++){
 $page=$pdf.GetPage($i);$stream=[Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
 try{
  $options=[Windows.Data.Pdf.PdfPageRenderOptions,Windows.Data.Pdf,ContentType=WindowsRuntime]::new();$options.DestinationWidth=1800
  Await-Action ($page.RenderToStreamAsync($stream,$options));$stream.Seek(0)
  $decoder=Await-Op ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap=Await-Op ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  try{
   $result=Await-Op ($ocr.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
   $words=[Collections.Generic.List[object]]::new()
   foreach($line in $result.Lines){foreach($word in $line.Words){$r=$word.BoundingRect;$words.Add(@{text=$word.Text;x=$r.X;y=$r.Y;w=$r.Width;h=$r.Height})}}
   $pages.Add(@{page=$i+1;text=$result.Text;words=$words.ToArray();method='windows_ocr';width=$bitmap.PixelWidth;height=$bitmap.PixelHeight})
  }finally{$bitmap.Dispose()}
 }finally{$stream.Dispose();$page.Dispose()}
}
[IO.File]::WriteAllText($OutputPath,(@{pages=$pages.ToArray();language=$ocr.RecognizerLanguage.LanguageTag}|ConvertTo-Json -Depth 8 -Compress),[Text.UTF8Encoding]::new($false))
