param([ValidateSet('Check','Start','Stop','Build','Backup','Restore')][string]$Action='Start',[switch]$Open,[string]$BackupFile,[switch]$ConfirmReplace)
$ErrorActionPreference='Stop'
$project=Split-Path $PSScriptRoot -Parent
$local=Join-Path $project '.local'
$stateFile=Join-Path $local 'state.json'
$jar=Join-Path $project 'backend/target/juxia-1.0.0.jar'

function Find-Java {
    $paths=@()
    if($env:JUXIA_JAVA_HOME){$paths+=Join-Path $env:JUXIA_JAVA_HOME 'bin/java.exe'}
    $paths+=Join-Path $project 'tools/jdk/bin/java.exe'
    if($env:JAVA_HOME){$paths+=Join-Path $env:JAVA_HOME 'bin/java.exe'}
    $paths+=Join-Path $env:USERPROFILE 'scoop/apps/temurin17-jdk/current/bin/java.exe'
    foreach($candidate in $paths){
        if(!(Test-Path -LiteralPath $candidate)){continue}
        $release=Join-Path (Split-Path (Split-Path $candidate)) 'release'
        if((Test-Path -LiteralPath $release) -and ((Get-Content -LiteralPath $release -Raw) -match 'JAVA_VERSION="(\d+)') -and [int]$Matches[1] -ge 17){return $candidate}
    }
    throw 'JDK 17+ is required. Set JUXIA_JAVA_HOME or place JDK in tools/jdk.'
}
function Find-MySql {
    $paths=@()
    if($env:JUXIA_MYSQL_HOME){$paths+=Join-Path $env:JUXIA_MYSQL_HOME 'bin/mysqld.exe'}
    $paths+=Join-Path $project 'tools/mysql/bin/mysqld.exe'
    $paths+=Join-Path $env:ProgramFiles 'MySQL/MySQL Server 8.0/bin/mysqld.exe'
    foreach($candidate in $paths){if(Test-Path -LiteralPath $candidate){return $candidate}}
    throw 'MySQL 8.0 server binaries are required. Set JUXIA_MYSQL_HOME or use tools/mysql.'
}
function Free-Port([int]$start){
    for($number=$start;$number -lt $start+80;$number++){
        $listener=New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback,$number)
        try{$listener.Start();return $number}catch{}finally{$listener.Stop()}
    }
    throw 'No free local port was found.'
}
function Save-State($state){$state|ConvertTo-Json|Set-Content -LiteralPath $stateFile -Encoding UTF8}
function New-Password {return [Guid]::NewGuid().ToString('N')}
function Database-Directory {
    # A real ASCII directory avoids Windows MySQL failures on Unicode data paths.
    $hasher=[Security.Cryptography.SHA256]::Create()
    try{$hash=([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($project)))).Replace('-','').Substring(0,12)}finally{$hasher.Dispose()}
    $directory=Join-Path ([Environment]::GetFolderPath('CommonDocuments')) ('GraduationRuntime\juxia-'+$hash)
    if($directory -match '[^\x00-\x7F]'){throw 'An ASCII path is required for the project MySQL data directory.'}
    $owner=Join-Path $directory 'owner.txt'
    if(Test-Path -LiteralPath $directory){
        if(!(Test-Path -LiteralPath $owner) -or [IO.File]::ReadAllText($owner) -ne $project){throw 'Database directory ownership differs. No data was changed.'}
    }else{
        New-Item -ItemType Directory -Path $directory -Force|Out-Null
        [IO.File]::WriteAllText($owner,$project,(New-Object Text.UTF8Encoding($false)))
    }
    return $directory
}
function Owned-Database($state){
    if(!$state.dbPid -or !$state.PSObject.Properties['dbConfig']){return $false}
    $process=Get-CimInstance Win32_Process -Filter "ProcessId=$($state.dbPid)" -ErrorAction SilentlyContinue
    return $process -and $process.CommandLine -and $process.CommandLine.Contains($state.dbConfig)
}
function Invoke-Sql([string]$sql,[string]$password,$state,[string]$user='root'){
    $mysql=Join-Path (Split-Path (Find-MySql)) 'mysql.exe'
    $info=New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName=$mysql
    $info.Arguments="--no-defaults --protocol=TCP -h127.0.0.1 -P$($state.dbPort) -u$user --default-character-set=utf8mb4 --batch --skip-column-names"
    $info.UseShellExecute=$false;$info.CreateNoWindow=$true
    $info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $info.StandardOutputEncoding=New-Object Text.UTF8Encoding($false)
    $info.StandardErrorEncoding=New-Object Text.UTF8Encoding($false)
    if($password){$info.EnvironmentVariables['MYSQL_PWD']=$password}
    else{$info.EnvironmentVariables.Remove('MYSQL_PWD')}
    $process=New-Object System.Diagnostics.Process;$process.StartInfo=$info
    [void]$process.Start()
    # Windows PowerShell 5.1 has no StandardInputEncoding property. Write UTF-8 bytes directly.
    $bytes=[Text.Encoding]::UTF8.GetBytes($sql+"`n")
    $process.StandardInput.BaseStream.Write($bytes,0,$bytes.Length)
    $process.StandardInput.BaseStream.Flush();$process.StandardInput.Close()
    $output=$process.StandardOutput.ReadToEnd();$errors=$process.StandardError.ReadToEnd();$process.WaitForExit()
    if($process.ExitCode -ne 0){throw 'Database command failed. Check the project database log and private state.'}
    return $output
}
function Owned-App($state){
    if(!$state.appPid){return $false}
    $process=Get-CimInstance Win32_Process -Filter "ProcessId=$($state.appPid)" -ErrorAction SilentlyContinue
    if(!$process -or !$process.CommandLine){return $false}
    return $process.CommandLine.Contains($jar)
}

try{
    if($Action -eq 'Check'){
        Write-Host ('Java: '+(Find-Java));Write-Host ('MySQL: '+(Find-MySql))
        Write-Host ('Packaged application present: '+(Test-Path -LiteralPath $jar))
        Write-Host 'Check does not create databases or start services.'
        exit 0
    }
    if($Action -eq 'Build'){
        if(Test-Path -LiteralPath $stateFile){
            $existing=Get-Content -LiteralPath $stateFile -Raw|ConvertFrom-Json
            if(Owned-App $existing){throw 'Stop this project before rebuilding; Windows locks a running JAR.'}
        }
        $java=Find-Java;$env:JAVA_HOME=Split-Path (Split-Path $java);$env:PATH=(Split-Path $java)+';'+$env:PATH
        $maven=Join-Path $project '.local/tools/Apache-Maven3.9.6/bin/mvn.cmd'
        if($env:MAVEN_HOME){$maven=Join-Path $env:MAVEN_HOME 'bin/mvn.cmd'}
        if(!(Test-Path -LiteralPath $maven)){$maven=(Get-Command mvn.cmd -ErrorAction Stop).Source}
        Push-Location (Join-Path $project 'frontend')
        try{& npm.cmd ci --no-audit --no-fund;if($LASTEXITCODE -ne 0){throw 'Frontend dependencies failed.'}; & npm.cmd run build;if($LASTEXITCODE -ne 0){throw 'Frontend build failed.'}}finally{Pop-Location}
        Push-Location (Join-Path $project 'backend')
        try{& $maven -B test package;if($LASTEXITCODE -ne 0){throw 'Backend build or tests failed.'}}finally{Pop-Location}
        Write-Host 'Build complete.';exit 0
    }
    if($Action -eq 'Stop'){
        if(!(Test-Path -LiteralPath $stateFile)){Write-Host 'This project has not been started.';exit 0}
        $state=Get-Content -LiteralPath $stateFile -Raw|ConvertFrom-Json
        if(Owned-App $state){Stop-Process -Id $state.appPid -ErrorAction Stop;$state.appPid=0}
        if(Owned-Database $state){[void](Invoke-Sql 'SHUTDOWN;' $state.rootPassword $state);$state.dbPid=0}
        Save-State $state;Write-Host 'Project services stopped; data has been kept.';exit 0
    }
    if($Action -eq 'Backup'){
        if(!(Test-Path -LiteralPath $stateFile)){throw 'Start this project before backing it up.'}
        $state=Get-Content -LiteralPath $stateFile -Raw|ConvertFrom-Json
        $backups=Join-Path $local 'backups';New-Item -ItemType Directory -Path $backups -Force|Out-Null
        $dump=Join-Path (Split-Path (Find-MySql)) 'mysqldump.exe'
        $backup=Join-Path $backups ((Get-Date -Format 'yyyyMMdd-HHmmss')+'.sql')
        $previous=$env:MYSQL_PWD
        try{$env:MYSQL_PWD=$state.rootPassword;& $dump --no-defaults --protocol=TCP '--host=127.0.0.1' "--port=$($state.dbPort)" --user=root --single-transaction --set-gtid-purged=OFF --default-character-set=utf8mb4 "--result-file=$backup" juxia;if($LASTEXITCODE -ne 0){throw 'Backup failed.'}}finally{$env:MYSQL_PWD=$previous}
        Write-Host ('Backup: '+$backup);exit 0
    }
    if($Action -eq 'Restore'){
        if(!$ConfirmReplace){throw 'Restore replaces current project data. Supply -BackupFile and -ConfirmReplace after checking the backup.'}
        if(!$BackupFile -or !(Test-Path -LiteralPath $BackupFile -PathType Leaf)){throw 'Specify an existing SQL backup file.'}
        $file=Get-Item -LiteralPath $BackupFile
        if($file.Extension -ne '.sql' -or $file.Length -gt 100MB){throw 'Restore accepts SQL backups up to 100 MB.'}
        $state=Get-Content -LiteralPath $stateFile -Raw|ConvertFrom-Json
        if(!(Owned-Database $state)){throw 'Start this project before restoring; its owned database must be running.'}
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Action Backup
        if($LASTEXITCODE -ne 0){throw 'Pre-restore backup failed; no data was replaced.'}
        if(Owned-App $state){Stop-Process -Id $state.appPid -ErrorAction Stop;$state.appPid=0;Save-State $state}
        $sql=[IO.File]::ReadAllText($file.FullName,[Text.Encoding]::UTF8)
        # This account has permissions only on juxia.*, never on another project's schema.
        [void](Invoke-Sql ("USE juxia;`n"+$sql) $state.dbPassword $state 'juxia')
        Write-Host 'Backup restored. The pre-restore backup remains in .local/backups.'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Action Start
        exit $LASTEXITCODE
    }
    $java=Find-Java;$mysqld=Find-MySql
    if(!(Test-Path -LiteralPath $jar)){throw 'Build the application before starting it.'}
    New-Item -ItemType Directory -Path $local -Force|Out-Null
    if(Test-Path -LiteralPath $stateFile){$state=Get-Content -LiteralPath $stateFile -Raw|ConvertFrom-Json}
    else{
        $state=[pscustomobject]@{appPort=(Free-Port 8108);dbPort=(Free-Port 3328);rootPassword=(New-Password);dbPassword=(New-Password);appPid=0;dbPid=0;initialized=$false}
        Save-State $state
    }
    if(Owned-App $state){
        $health=Invoke-RestMethod -Uri "http://127.0.0.1:$($state.appPort)/api/health" -TimeoutSec 5
        if($health.name -eq '剧匣' -and $health.databaseReady){Write-Host "Already running: http://127.0.0.1:$($state.appPort)";if($Open){Start-Process "http://127.0.0.1:$($state.appPort)"};exit 0}
        throw 'The existing project process is not ready. Check .local/app.err.log.'
    }
    $dataRoot=Database-Directory
    $data=Join-Path $dataRoot 'data';$base=Split-Path (Split-Path $mysqld)
    $state|Add-Member -NotePropertyName dataRoot -NotePropertyValue $dataRoot -Force
    Save-State $state
    if(!(Test-Path -LiteralPath (Join-Path $data 'mysql'))){
        if((Test-Path -LiteralPath $data) -and (Get-ChildItem -LiteralPath $data -Force|Select-Object -First 1)){throw 'Partial MySQL data exists. Review it manually; the launcher does not delete data.'}
        New-Item -ItemType Directory -Path $data -Force|Out-Null
        $init=Start-Process -FilePath $mysqld -ArgumentList @('--no-defaults',"--basedir=`"$base`"","--datadir=`"$data`"",'--initialize-insecure','--console') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $local 'mysql-init.log') -RedirectStandardError (Join-Path $local 'mysql-init.err.log')
        if($init.ExitCode -ne 0){throw 'MySQL initialization failed. See .local/mysql-init.err.log.'}
    }
    $configPath=Join-Path $dataRoot 'mysql.ini'
    $ownedDb=Owned-Database $state
    if(!$ownedDb){
        $state.dbPort=Free-Port ([int]$state.dbPort)
        $config="[mysqld]`r`nbasedir="+$base.Replace('\','/')+"`r`ndatadir="+$data.Replace('\','/')+"`r`nport=$($state.dbPort)`r`nbind-address=127.0.0.1`r`nmysqlx=0`r`nskip-log-bin`r`ncharacter-set-server=utf8mb4`r`ncollation-server=utf8mb4_unicode_ci`r`n"
        [IO.File]::WriteAllText($configPath,$config,(New-Object Text.UTF8Encoding($false)))
        $dbProcess=Start-Process -FilePath $mysqld -ArgumentList ('--defaults-file="'+$configPath+'"') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $local 'mysql.out.log') -RedirectStandardError (Join-Path $local 'mysql.err.log')
        $state|Add-Member -NotePropertyName dbConfig -NotePropertyValue $configPath -Force
        $state.dbPid=$dbProcess.Id;Save-State $state
    }
    $ready=$false
    for($attempt=0;$attempt -lt 40;$attempt++){
        try{$auth='';if($state.initialized){$auth=$state.rootPassword};[void](Invoke-Sql 'SELECT 1;' $auth $state);$ready=$true;break}catch{Start-Sleep -Milliseconds 700}
    }
    if(!$ready){throw 'MySQL did not become ready. Check .local/mysql.err.log.'}
    if(!$state.initialized){
        $sql="CREATE DATABASE IF NOT EXISTS juxia CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER IF NOT EXISTS 'juxia'@'127.0.0.1' IDENTIFIED BY '$($state.dbPassword)'; GRANT ALL PRIVILEGES ON juxia.* TO 'juxia'@'127.0.0.1'; ALTER USER 'root'@'localhost' IDENTIFIED BY '$($state.rootPassword)';"
        try{[void](Invoke-Sql $sql '' $state)}catch{[void](Invoke-Sql $sql $state.rootPassword $state)}
        $state.initialized=$true;Save-State $state
    }
    $state.appPort=Free-Port ([int]$state.appPort);Save-State $state
    @("server.port=$($state.appPort)","spring.datasource.url=jdbc:mysql://127.0.0.1:$($state.dbPort)/juxia?useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai","spring.datasource.username=juxia","spring.datasource.password=$($state.dbPassword)")|Set-Content -LiteralPath (Join-Path $local 'runtime.properties') -Encoding ASCII
    $app=Start-Process -FilePath $java -ArgumentList @('-Dfile.encoding=UTF-8','-jar',"`"$jar`"") -WorkingDirectory $project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $local 'app.log') -RedirectStandardError (Join-Path $local 'app.err.log')
    $state.appPid=$app.Id;Save-State $state
    for($attempt=0;$attempt -lt 60;$attempt++){
        try{$health=Invoke-RestMethod -Uri "http://127.0.0.1:$($state.appPort)/api/health" -TimeoutSec 2;if($health.databaseReady){Write-Host "Ready: http://127.0.0.1:$($state.appPort)";if($Open){Start-Process "http://127.0.0.1:$($state.appPort)"};exit 0}}catch{}
        if($app.HasExited){throw 'Application exited. See .local/app.log and app.err.log.'}
        Start-Sleep -Milliseconds 700
    }
    throw 'Application readiness timed out. See .local/app.log.'
}catch{
    Write-Host ('ERROR: '+$_.Exception.Message) -ForegroundColor Red
    exit 1
}
