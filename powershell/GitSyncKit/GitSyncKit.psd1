@{
    RootModule        = 'GitSyncKit.psm1'
    ModuleVersion     = '0.1.0'
    GUID              = 'a7c3e1f2-5b84-4d61-9e07-2f8d4c6b1a35'
    Author            = 'CalBankrupt'
    CompanyName       = 'CalBankrupt'
    Copyright         = '(c) CalBankrupt. MIT License.'
    Description       = 'git-sync-kit 的 PowerShell 实现：检查更新 / 安装更新 / 差异清单 / 选择性应用 / 双向同步。行为契约见仓库根的 SPEC.md。'
    # 只支持 PowerShell 7+：5.1 的默认执行策略常常拒绝加载模块，而退出码
    # 仍是 0，表现为「命令跑完但什么都没输出」，排查成本比不支持更高。
    PowerShellVersion = '7.0'

    FunctionsToExport = @(
        'Get-GitSyncRoot'
        'Get-GitSyncReport'
        'Invoke-GitSyncInstall'
        'Get-GitSyncDiff'
        'Invoke-GitSyncApply'
        'Invoke-GitSyncPush'
    )
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()

    PrivateData = @{
        PSData = @{
            Tags       = @('git', 'update', 'sync', 'toolkit')
            ProjectUri = 'https://github.com/CalBankrupt/git-sync-kit'
            LicenseUri = 'https://github.com/CalBankrupt/git-sync-kit/blob/main/LICENSE'
        }
    }
}
