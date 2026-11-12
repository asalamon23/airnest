Dantherm HCV 400 Windows executable build package

This package is source code, NOT a precompiled EXE.

To get a Windows EXE without installing Python:
1. Create a private GitHub repository.
2. Upload the files in this folder, including .github/workflows/windows-exe.yml
   (preserve the .github folder structure).
3. Open the repository's Actions tab.
4. Select 'Build Windows EXE' and click 'Run workflow'.
5. Download the 'Dantherm-HCV400-Windows' artifact from the completed run.
6. Unzip it and double-click Dantherm-HCV400.exe on your Windows PC.
7. Keep the console window open while using the dashboard.

The app opens http://localhost:8088 and connects to 192.168.1.142:502.
Both the PC and ventilation unit must be on the same LAN.
CSV data is written next to the EXE. This is read-only experimental software.
If Windows Defender/SmartScreen flags the unsigned EXE, inspect the source
and build it yourself rather than bypassing security warnings blindly.
