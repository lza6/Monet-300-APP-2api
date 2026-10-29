@echo off
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 (echo VCVARS_FAILED & exit /b 1)
where cl
cd /d D:\apk-analysis\blutter
python D:\apk-analysis\blutter\blutter.py D:\apk-analysis\monet300\apktool_out\lib\arm64-v8a D:\apk-analysis\monet300\blutter_out
echo BLUTTER_EXIT=%ERRORLEVEL%
