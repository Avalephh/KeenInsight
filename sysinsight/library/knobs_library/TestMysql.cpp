#include "tainter.h"
#include "llvm/IR/Module.h"

int main(int argc, char **argv) {
  string ir_file = string(argv[1]);  // input the ir path
  string var_file = string(argv[2]); // input the variable mapping path
  std::vector<struct ConfigVariableNameInfo *> config_names;
  if (!readConfigVariableNames(var_file, config_names))
    exit(1);

  // std::unique_ptr<llvm::Module> module;
  LLVMContext context;
  std::unique_ptr<llvm::Module> module;
  SMDiagnostic Err;
  buildModule(module, context, Err,
              ir_file); // build the llvm module based on ir
  std::vector<struct GlobalVariableInfo *> gvlist = getGlobalVariableInfo(
      module, config_names);         // conduct Configuration Variable Mapping
  startAnalysis(gvlist, true, true); // Taint Analysis with indirect data-flow,
                                     // and indirect control-flow

  string output_file = var_file.substr(0, var_file.find_last_of(".")) +
                       "-ControlDependency-records.dat";
  ofstream fout(output_file, ios::app);
  //   std::string output_content = "";

  for (auto i = gvlist.begin(), e = gvlist.end(); i != e; i++) {
    struct GlobalVariableInfo *gv_info = *i;
    vector<struct InstInfo *> ExplicitDataFlow = gv_info->getExplicitDataFlow();
    llvm::outs() << "getExplicitDataFlow size= " << ExplicitDataFlow.size()
                 << "\n";
    fout << "Global Variable: " << gv_info->NameInfo->getNameAsString() << "\n";
    fout << "Explicit Data Flow (" << ExplicitDataFlow.size() << "):\n";
    for (auto &inst : ExplicitDataFlow) {
      fout << "  Instruction: " << getAsString(inst->InstPtr)
           << " | Location: " << inst->InstLoc.toString() << " | Function: "
           << getOriginalName(inst->InstPtr->getFunction()->getName().str())
           << "\n";
    }
    fout << "\n";
    vector<struct InstInfo *> ImplicitDataFlow = gv_info->getImplicitDataFlow();
    llvm::outs() << "getImplicitDataFlow size= " << ImplicitDataFlow.size()
                 << "\n";
    fout << "Implicit Data Flow (" << ImplicitDataFlow.size() << "):\n";
    for (auto &inst : ImplicitDataFlow) {
      fout << "  Instruction: " << getAsString(inst->InstPtr)
           << " | Location: " << inst->InstLoc.toString() << getOriginalName(inst->InstPtr->getFunction()->getName().str()) << "\n";           
    }
    fout << "\n";

    fout << "----------------------------------------\n";

    fout << "Explicit Control Flow:\n";
    for (auto &inst : ExplicitDataFlow) {
      struct InstInfo *k1 = inst;
      vector<Function *> cFuncs = k1->getControlledFunction();
      for (auto &func : cFuncs) {
        std::string mName = func->getName().str();
        std::string dname = getOriginalName(mName);
        fout << "  Controlled Function: " << dname << "\n";
      }
    }
    fout << "\n";

    fout << "Implicit Control Flow:\n";
    for (auto &inst : ImplicitDataFlow) {
      struct InstInfo *k1 = inst;
      vector<BasicBlock *> cBlocks = k1->getImplicitControllingBBs();
      for (auto &bb : cBlocks) {
        fout << "  Controlled BasicBlock: " << bb->getName().str() << "\n";
      }
    }
    fout << "\n";
    fout << "----------------------------------------\n";
  }
  // 关闭文件
  fout.close();
  fout.clear();

  std::cout << "数据流信息已成功写入到文件: " << output_file << std::endl;

  return 0;
}