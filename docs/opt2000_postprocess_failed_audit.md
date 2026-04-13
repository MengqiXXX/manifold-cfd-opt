# opt2000：POSTPROCESS_FAILED 抽样审计（10例）

- 数据库：`/home/liumq/opt_runs/opt2000/results_opt_2000.sqlite`
- 抽样数：`10`（按 rowid 最小的前 N 条）

## 统计结论

- 无时间步目录（无 `1/2/…` 或 `0.5/…`）：`10/10`
- 缺失 `log.solver`：`10/10`
- `reconstructPar` 提示无可用时间步并选择 constant：`10/10`
- 缺失 `postProcessing/`：`10/10`

**missing 字段出现频次（来自 failure.missing）**

- `outlet1Flow`: 10
- `outlet1P`: 10

## 样本证据（逐例）

| rowid | logits | case_dir | time_dirs | has log.solver | has postProcessing | reconstructPar 关键提示 |
|---:|---|---|---|---|---|---|
| 1 | 1.990, -1.583, 1.292 | /home/liumq/manifold_cases/manifold_1775957786_2753546/case |  | N | N | No time… selecting constant |
| 2 | -1.109, 0.948, -0.209 | /home/liumq/manifold_cases/manifold_1775957786_5827755/case |  | N | N | No time… selecting constant |
| 3 | -0.076, -0.575, 0.042 | /home/liumq/manifold_cases/manifold_1775957786_5894772/case |  | N | N | No time… selecting constant |
| 4 | 0.957, 1.955, -1.459 | /home/liumq/manifold_cases/manifold_1775957786_5555545/case |  | N | N | No time… selecting constant |
| 5 | 0.187, -0.155, -0.872 | /home/liumq/manifold_cases/manifold_1775957786_0671797/case |  | N | N | No time… selecting constant |
| 6 | -0.787, 1.250, 1.627 | /home/liumq/manifold_cases/manifold_1775957786_9180989/case |  | N | N | No time… selecting constant |
| 7 | -1.754, -1.131, -1.622 | /home/liumq/manifold_cases/manifold_1775957786_3868369/case |  | N | N | No time… selecting constant |
| 8 | 1.155, 0.274, 0.877 | /home/liumq/manifold_cases/manifold_1775957786_4585571/case |  | N | N | No time… selecting constant |
| 9 | 1.415, -0.841, -1.947 | /home/liumq/manifold_cases/manifold_1775957801_9260114/case |  | N | N | No time… selecting constant |
| 10 | -1.549, 1.691, 0.552 | /home/liumq/manifold_cases/manifold_1775957801_0472181/case |  | N | N | No time… selecting constant |

## 代表性证据片段（节选）

### rowid 1

**reconstructPar tail**

```
/*---------------------------------------------------------------------------*\
  =========                 |
  \\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
   \\    /   O peration     | Website:  https://openfoam.org
    \\  /    A nd           | Version:  13
     \\/     M anipulation  |
\*---------------------------------------------------------------------------*/
Build  : 13-58ed5c2046ef
Exec   : reconstructPar -latestTime
Date   : Apr 12 2026
Time   : 01:36:41
Host   : "ps"
PID    : 1118828
I/O    : uncollated
Case   : /home/liumq/manifold_cases/manifold_1775957786_2753546/case
nProcs : 1
sigFpe : Enabling floating point exception trapping (FOAM_SIGFPE).
fileModificationChecking : Monitoring run-time modified files using timeStampMaster (fileModificationSkew 10)
allowSystemOperations : Allowing user-supplied system call operations

// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //
Create time

--> FOAM Warning : 
    From function static Foam::instantList Foam::timeSelector::select0(Foam::Time&, const Foam::argList&)
    in file db/Time/timeSelector.C at line 269
    No time specified or available, selecting 'constant'
Time = constant

Reconstructing FV fields

    (no FV fields)

Reconstructing point fields

    (no point fields)

End

```

### rowid 2

**reconstructPar tail**

```
/*---------------------------------------------------------------------------*\
  =========                 |
  \\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
   \\    /   O peration     | Website:  https://openfoam.org
    \\  /    A nd           | Version:  13
     \\/     M anipulation  |
\*---------------------------------------------------------------------------*/
Build  : 13-58ed5c2046ef
Exec   : reconstructPar -latestTime
Date   : Apr 12 2026
Time   : 01:36:41
Host   : "ps"
PID    : 1119691
I/O    : uncollated
Case   : /home/liumq/manifold_cases/manifold_1775957786_5827755/case
nProcs : 1
sigFpe : Enabling floating point exception trapping (FOAM_SIGFPE).
fileModificationChecking : Monitoring run-time modified files using timeStampMaster (fileModificationSkew 10)
allowSystemOperations : Allowing user-supplied system call operations

// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //
Create time

--> FOAM Warning : 
    From function static Foam::instantList Foam::timeSelector::select0(Foam::Time&, const Foam::argList&)
    in file db/Time/timeSelector.C at line 269
    No time specified or available, selecting 'constant'
Time = constant

Reconstructing FV fields

    (no FV fields)

Reconstructing point fields

    (no point fields)

End

```

### rowid 3

**reconstructPar tail**

```
/*---------------------------------------------------------------------------*\
  =========                 |
  \\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
   \\    /   O peration     | Website:  https://openfoam.org
    \\  /    A nd           | Version:  13
     \\/     M anipulation  |
\*---------------------------------------------------------------------------*/
Build  : 13-58ed5c2046ef
Exec   : reconstructPar -latestTime
Date   : Apr 12 2026
Time   : 01:36:41
Host   : "ps"
PID    : 1119124
I/O    : uncollated
Case   : /home/liumq/manifold_cases/manifold_1775957786_5894772/case
nProcs : 1
sigFpe : Enabling floating point exception trapping (FOAM_SIGFPE).
fileModificationChecking : Monitoring run-time modified files using timeStampMaster (fileModificationSkew 10)
allowSystemOperations : Allowing user-supplied system call operations

// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //
Create time

--> FOAM Warning : 
    From function static Foam::instantList Foam::timeSelector::select0(Foam::Time&, const Foam::argList&)
    in file db/Time/timeSelector.C at line 269
    No time specified or available, selecting 'constant'
Time = constant

Reconstructing FV fields

    (no FV fields)

Reconstructing point fields

    (no point fields)

End

```

