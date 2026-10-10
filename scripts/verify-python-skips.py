"""Run Python regressions and record each skip; release mode rejects all skips."""
import argparse,json,sys,unittest
from pathlib import Path

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument('--out',required=True)
 parser.add_argument('--pattern',default='*_test.py')
 parser.add_argument('--release',action='store_true',help='Fail closed on any skipped check')
 args=parser.parse_args()
 root=Path(__file__).resolve().parents[1]
 sys.path.insert(0,str(root))
 suite=unittest.defaultTestLoader.discover(str(root/'tests'),pattern=args.pattern)
 class Result(unittest.TextTestResult):
  def addSkip(self,test,reason):
   super().addSkip(test,reason)
 result=unittest.TextTestRunner(verbosity=2,resultclass=Result).run(suite)
 report={'schemaVersion':1,'testsRun':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
         'skipped':len(result.skipped),'skips':[{'test':test.id(),'reason':reason,'releaseRequired':True} for test,reason in result.skipped],
         'regressionPassed':result.wasSuccessful(),'releaseSkipGatePassed':result.wasSuccessful() and not result.skipped,
         'cleanEnvironmentVerified':False,'releaseMode':args.release}
 out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
 out.write_text(json.dumps(report,indent=2,ensure_ascii=True)+'\n',encoding='utf8')
 print(json.dumps({k:v for k,v in report.items() if k!='skips'}))
 return 0 if result.wasSuccessful() and (not args.release or not result.skipped) else 1
if __name__=='__main__':raise SystemExit(main())
