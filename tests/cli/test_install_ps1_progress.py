"""Contracts for the PowerShell installer progress helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from http import HTTPStatus
from pathlib import Path

import pytest

INSTALL_PS1 = Path(__file__).parents[2] / "install.ps1"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def test_install_ps1_defines_branded_progress_helpers() -> None:
    source = INSTALL_PS1.read_text()

    for helper in (
        "function Write-OpenSreHeader",
        "function Test-OpenSreInteractiveHost",
        "function Get-OpenSreConsoleWidth",
        "function Limit-OpenSreText",
        "function Get-OpenSreFriendlyProgressLabel",
        "function Get-OpenSreProgressFrame",
        "function New-OpenSreProgressBar",
        "function Invoke-OpenSreStep",
        "function Invoke-OpenSreFirstLaunchWarmup",
        "function Invoke-OpenSreDownloadFileWithProgress",
    ):
        assert helper in source

    assert "OPENSRE_INSTALL_VERBOSE" in source
    assert '$ProgressPreference = "SilentlyContinue"' in source
    assert "$ProgressPreference = $previousProgressPreference" in source
    assert "Clear-Host" not in source
    assert "preparing installer" not in source


def test_install_ps1_avoids_ps7_only_syntax_and_write_progress() -> None:
    source = INSTALL_PS1.read_text()

    forbidden_snippets = (
        "$PSStyle",
        "??",
        "Join-String",
        "-SkipHttpErrorCheck",
        "Write-Progress",
    )
    for snippet in forbidden_snippets:
        assert snippet not in source


def test_install_ps1_preserves_retry_contract_source() -> None:
    source = INSTALL_PS1.read_text()

    assert 'Write-Warning "Attempt $attempt to $Description failed' in source
    assert "after $attempt attempts" in source


def test_install_ps1_api_token_is_not_sent_to_asset_downloads() -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")

    script = textwrap.dedent(
        f"""
        . '{INSTALL_PS1}' -SkipMain
        $env:GH_TOKEN = 'test-token'
        $env:GITHUB_TOKEN = 'other-token'
        $api = Get-OpenSreApiRequestHeaders
        $asset = Get-OpenSreRequestHeaders
        if ($api.Authorization -ne 'Bearer test-token') {{ throw 'API token missing' }}
        if ($asset.ContainsKey('Authorization')) {{ throw 'Token leaked to asset request' }}
        function Invoke-RestMethod {{
            param($Uri, $Headers)
            $script:metadataHeaders = $Headers
            return @{{ tag_name = 'v1' }}
        }}
        function Invoke-WebRequest {{
            param($Uri, $Headers, $OutFile)
            $script:assetHeaders = $Headers
        }}
        Invoke-OpenSreRestMethod -Uri 'https://api.github.com/example' | Out-Null
        Invoke-OpenSreDownloadFileWithProgress -Uri 'https://github.com/example.zip' -OutFile 'unused.zip'
        if ($script:metadataHeaders.Authorization -ne 'Bearer test-token') {{ throw 'Metadata request missing token' }}
        if ($script:assetHeaders.ContainsKey('Authorization')) {{ throw 'Asset request leaked token' }}
        $env:GH_TOKEN = ''
        if ((Get-OpenSreApiRequestHeaders).Authorization -ne 'Bearer other-token') {{ throw 'GITHUB_TOKEN fallback missing' }}
        Write-Output 'HEADERS_OK'
        """
    )
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "HEADERS_OK" in result.stdout


@pytest.mark.parametrize("status", [HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS])
def test_install_ps1_rate_limit_error_is_actionable(status: HTTPStatus) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")

    script = textwrap.dedent(
        f"""
        . '{INSTALL_PS1}' -SkipMain
        $script:attempts = 0
        try {{
            Invoke-OpenSreWithRetry -Description 'fetch release metadata from GitHub' -Operation {{
                $script:attempts++
                $failure = New-Object System.Exception 'rate limited'
                $failure | Add-Member -NotePropertyName StatusCode -NotePropertyValue {status.value}
                throw $failure
            }} | Out-Null
            throw 'Expected rate-limit failure'
        }}
        catch {{
            if ($_.Exception.Message -notmatch 'HTTP {status.value}.*GH_TOKEN') {{ throw }}
            if ($script:attempts -ne 1) {{ throw 'Rate limit was retried' }}
        }}
        Write-Output 'RATE_LIMIT_OK'
        """
    )
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "RATE_LIMIT_OK" in result.stdout


def test_install_ps1_invalid_token_retries_release_metadata_anonymously() -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")

    script = textwrap.dedent(
        f"""
        . '{INSTALL_PS1}' -SkipMain
        $env:GH_TOKEN = 'expired-token'
        $script:attempts = 0
        function Invoke-RestMethod {{
            param($Uri, $Headers)
            $script:attempts++
            if ($Headers.ContainsKey('Authorization')) {{
                $failure = New-Object System.Exception 'invalid token'
                $failure | Add-Member -NotePropertyName StatusCode -NotePropertyValue ([int][System.Net.HttpStatusCode]::Unauthorized)
                throw $failure
            }}
            return @{{ tag_name = 'v1' }}
        }}
        $release = Invoke-OpenSreRestMethod -Uri 'https://api.github.com/example'
        if ($release.tag_name -ne 'v1') {{ throw 'Anonymous fallback failed' }}
        if ($script:attempts -ne 2) {{ throw 'Expected one token request and one anonymous request' }}
        Write-Output 'ANONYMOUS_FALLBACK_OK'
        """
    )
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ANONYMOUS_FALLBACK_OK" in result.stdout


def test_install_ps1_rate_limited_after_rejected_token_recommends_replacement() -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")

    script = textwrap.dedent(
        f"""
        . '{INSTALL_PS1}' -SkipMain
        $env:GH_TOKEN = 'expired-token'
        $script:attempts = 0
        function Invoke-RestMethod {{
            param($Uri, $Headers)
            $script:attempts++
            $status = if ($Headers.ContainsKey('Authorization')) {{
                [System.Net.HttpStatusCode]::Unauthorized
            }} else {{
                [System.Net.HttpStatusCode]::Forbidden
            }}
            $failure = New-Object System.Exception 'request denied'
            $failure | Add-Member -NotePropertyName StatusCode -NotePropertyValue ([int]$status)
            throw $failure
        }}
        try {{
            Invoke-OpenSreRestMethod -Uri 'https://api.github.com/example' | Out-Null
            throw 'Expected rate-limit failure'
        }}
        catch {{
            if ($_.Exception.Message -notmatch 'Replace any rejected token') {{ throw }}
            if ($script:attempts -ne 2) {{ throw 'Unexpected retry count' }}
        }}
        Write-Output 'REJECTED_TOKEN_GUIDANCE_OK'
        """
    )
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "REJECTED_TOKEN_GUIDANCE_OK" in result.stdout


def test_install_ps1_defaults_to_main_build_channel() -> None:
    source = INSTALL_PS1.read_text()

    assert 'else { "main" }' in source
    assert 'else { "main-build" }' in source
    assert "releases/tags/$mainReleaseTag" in source
    assert "$script:OpenSreChannelExplicit" in source
    assert '$resolvedChannel = "release"' in source
    assert "releases/tags/nightly" not in source


def test_install_ps1_contains_auto_onboarding_launch_hook() -> None:
    source = INSTALL_PS1.read_text()

    assert "function Test-OpenSreAutoLaunchEnabled" in source
    assert "function Start-OpenSreOnboardingAfterInstall" in source
    assert "OPENSRE_AUTO_LAUNCH" in source
    assert "& $BinaryPath setup" in source
    assert "Start-OpenSreOnboardingAfterInstall -BinaryPath $installedBinaryPath" in source
    # A redirected/piped host must be treated as non-interactive so the
    # full-screen prompt is not launched into a terminal it cannot control
    # (issue #3273).
    assert "[System.Console]::IsInputRedirected" in source


def test_install_ps1_records_install_analytics_without_blocking_install() -> None:
    source = INSTALL_PS1.read_text()

    assert "function Send-OpenSreInstallAnalytics" in source
    assert '$env:OPENSRE_INSTALL_SOURCE = "powershell_installer"' in source
    assert "& $BinaryPath --record-install *> $null" in source
    assert "Analytics is best-effort and must never fail installation." in source


@pytest.mark.parametrize("prior_marker", [False, True])
@pytest.mark.parametrize("wizard_override", [False, True])
def test_install_ps1_records_original_marker_and_restores_environment(
    tmp_path: Path, prior_marker: bool, wizard_override: bool
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    if prior_marker:
        (state_dir / "installed").touch()
    fake_binary = tmp_path / "binary.ps1"
    fake_binary.write_text(
        "$env:OPENSRE_INSTALL_MARKER_STATE | Set-Content -LiteralPath $env:OPENSRE_TEST_MARKER_LOG\n"
        'throw "Telemetry failed"\n'
    )
    recorded = tmp_path / "recorded"
    script = textwrap.dedent(
        f"""
        . '{str(INSTALL_PS1).replace("'", "''")}' -SkipMain
        $env:OPENSRE_INSTALL_MARKER_STATE = 'original'
        function Write-OpenSreHeader {{
            # Called by the real installer after its initial snapshot.
            $marker = Join-Path $env:OPENSRE_TEST_STATE_DIR 'installed'
            if (Test-Path -LiteralPath $marker) {{ Remove-Item -LiteralPath $marker }}
            else {{ New-Item -ItemType File -Path $marker | Out-Null }}
            Send-OpenSreInstallAnalytics -BinaryPath $env:OPENSRE_TEST_BINARY -Channel main -Version test -InstallMarkerState $installMarkerState
            if ($env:OPENSRE_INSTALL_MARKER_STATE -ne 'original') {{ throw 'Environment leaked' }}
            throw 'TEST_FINISHED'
        }}
        try {{ Install-OpenSre }}
        catch {{ if ($_.Exception.Message -ne 'TEST_FINISHED') {{ throw }} }}
        # A caught terminating error leaves pwsh -Command's exit status failed.
        # Report successful completion only after all test assertions ran.
        Write-Output 'INSTALLER_SNAPSHOT_OK'
        """
    )

    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        env=os.environ
        | {
            "OPENSRE_HOME": str(tmp_path / "unused home" if wizard_override else state_dir),
            "OPENSRE_WIZARD_STORE_PATH": str(state_dir / "wizard.json") if wizard_override else "",
            "OPENSRE_TEST_STATE_DIR": str(state_dir),
            "OPENSRE_TEST_BINARY": str(fake_binary),
            "OPENSRE_TEST_MARKER_LOG": str(recorded),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "INSTALLER_SNAPSHOT_OK" in result.stdout
    assert (state_dir / "installed").exists() is not prior_marker
    assert recorded.read_text(encoding="utf-8-sig").strip() == (
        "present" if prior_marker else "absent"
    )


def test_install_ps1_preserves_full_binary_name_in_next_steps() -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")

    script = textwrap.dedent(
        f"""
        . '{INSTALL_PS1}' -SkipMain
        Get-OpenSreCommandName -BinaryName 'opensre.exe'
        """
    )

    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "opensre"


def test_install_ps1_soft_installs_github_cli_via_winget() -> None:
    source = INSTALL_PS1.read_text()

    assert "function Ensure-OpenSreGithubCli" in source
    assert "OPENSRE_SKIP_GH_INSTALL" in source
    assert "winget install --id GitHub.cli" in source
    assert "Ensure-OpenSreGithubCli" in source


def test_install_ps1_keeps_download_urls_verbose_only() -> None:
    source = INSTALL_PS1.read_text()

    assert 'Write-OpenSreDetail -Message "Download URL: $Uri"' in source
    assert 'Write-OpenSreDetail -Message "Destination: $OutFile"' in source
    assert "-Detail $downloadUrl" not in source
    assert "-Detail $checksumUrl" not in source


def test_install_ps1_uses_bounded_short_progress_labels() -> None:
    source = INSTALL_PS1.read_text()

    assert "Get-OpenSreConsoleWidth" in source
    assert "Limit-OpenSreText -Text (Get-OpenSreFriendlyProgressLabel -Label $Label)" in source
    assert "Installing OpenSRE" in source
    assert "downloading archive" in source
    assert "verifying checksum" in source
    assert '" " * 100' not in source
    # The -f expression must be fully parenthesized before Console.Write, otherwise
    # PowerShell steals the second -f argument as a Write parameter (issue #4188).
    assert '[System.Console]::Write(("`r{0}`r{1}" -f (" " * $clearWidth), $content))' in source


def test_install_ps1_progress_format_survives_console_write_precedence() -> None:
    """Regression for #4188: Console.Write + -f comma precedence on Windows."""
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")

    # Mirror Write-OpenSreProgressLine's format call. The unparenthesized form
    # throws FormatException; the parenthesized form used in install.ps1 must succeed.
    script = textwrap.dedent(
        r"""
        $ErrorActionPreference = 'Stop'
        $clearWidth = 40
        $content = '  / #### Installing OpenSRE downloading archive 5%'
        [System.Console]::Write(("`r{0}`r{1}" -f (" " * $clearWidth), $content))
        Write-Output 'FORMAT_OK'
        """
    )

    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "FORMAT_OK" in (result.stdout + result.stderr)


def test_install_ps1_dot_sources_when_powershell_available() -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")

    script = textwrap.dedent(
        f"""
        . '{INSTALL_PS1}' -SkipMain
        Write-OpenSreHeader -Channel release -RequestedVersion '' -InstallDir 'C:\\opensre' -Repo 'Tracer-Cloud/opensre'
        Invoke-OpenSreStep -Name 'Unit progress step' -Operation {{ 'result-value' }}
        Write-OpenSreProgressLine -Label 'opensre_main_windows-arm64.zip.sha256' -DownloadedBytes 10 -TotalBytes 100
        Clear-OpenSreProgressLine
        """
    )

    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "OpenSRE installer" in output
    assert "Unit progress step" in output
    assert "OK Unit progress step" in output
    assert "result-value" in output


@pytest.mark.parametrize(
    ("tag", "origin"),
    [("-lp", "landing_page"), ("-gh", "github"), ("-dc", "documentation"), ("", "")],
)
def test_powershell_origin_reaches_binary_and_restores_environment(
    tmp_path: Path, tag: str, origin: str
) -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")
    fake_binary = tmp_path / "binary.ps1"
    fake_binary.write_text(
        "@{ origin = [string]$env:OPENSRE_INSTALL_ORIGIN; track = $env:OPENSRE_INSTALL_CHANNEL; source = $env:OPENSRE_INSTALL_SOURCE } | ConvertTo-Json | Set-Content -LiteralPath $env:OPENSRE_TEST_ORIGIN_LOG\n"
        "throw 'Telemetry failed'\n"
    )
    recorded = tmp_path / "recorded.json"
    script = f"""
        $source = Get-Content -Raw -LiteralPath '{str(INSTALL_PS1).replace("'", "''")}'
        $probe = @'
        $env:OPENSRE_INSTALL_ORIGIN = 'previous'
        Send-OpenSreInstallAnalytics -BinaryPath $env:OPENSRE_TEST_BINARY -Channel $Channel -Version test
        if ($env:OPENSRE_INSTALL_ORIGIN -ne 'previous') {{ throw 'Environment leaked' }}
        Write-Output 'ORIGIN_OK'
'@
        & ([scriptblock]::Create($source + "`n" + $probe)) -SkipMain -Channel release {tag}
    """
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        env=os.environ
        | {"OPENSRE_TEST_BINARY": str(fake_binary), "OPENSRE_TEST_ORIGIN_LOG": str(recorded)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ORIGIN_OK" in result.stdout
    assert json.loads(recorded.read_text(encoding="utf-8-sig")) == {
        "origin": origin,
        "track": "release",
        "source": "powershell_installer",
    }


def test_powershell_rejects_conflicting_origins() -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell is not installed in this environment.")
    result = subprocess.run(
        [
            shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(INSTALL_PS1),
            "-SkipMain",
            "-lp",
            "-gh",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "parameter set" in (result.stdout + result.stderr).lower()
