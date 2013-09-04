#------------------------------------------------------------------------------
# Copyright (c) 2013, Nucleic Development Team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
#------------------------------------------------------------------------------
from atom.api import Constant, List, Typed

from .code_generator import CodeGenerator
from .compiler_base import CompilerBase
from .compiler_util import (
    VarPool, needs_engine, needs_subclass,
)


class BlockCompiler(CompilerBase):
    """ A base class for creating block compilers.

    This class implements common logic for the enamldef and template
    compilers.

    """
    #: A variable name generator.
    var_pool = Typed(VarPool, ())

    #: The name of scope key in local storage.
    scope_key = Constant('_[scope_key]')

    #: A stack of var names for parent classes.
    class_stack = List()

    #: A stack of var names for parent nodes.
    node_stack = List()

    #: A stack of attr bind names for parent nodes.
    bind_stack = List()

    #: A stack of compiled code objects generated by visitors.
    code_stack = List()

    def load_name(self, name):
        """ Load the given name onto the TOS.

        This method must be implemented by subclasses.

        """
        raise NotImplementedError

    def has_locals(self):
        """ Get whether or not this block has locals.

        This method must be implemented by subclasses.

        """
        raise NotImplementedError

    def local_names(self):
        """ Get the set of local block names available to user code.

        This method must be implemented by subclasses.

        """
        raise NotImplementedError

    def prepare_block(self):
        """ Prepare the block for execution.

        This method must be invoked by subclasses.

        """
        cg = self.code_generator
        cg.store_globals_to_fast()
        cg.store_helpers_to_fast()
        cg.load_helper_from_fast('make_object')
        cg.call_function()
        cg.store_fast(self.scope_key)

    def visit_ChildDef(self, node):
        """ The compiler visitor for a ChildDef node.

        """
        cg = self.code_generator

        # Claim the variables needed for the class and construct node
        class_var = self.var_pool.new()
        node_var = self.var_pool.new()

        # Set the line number and load the child class
        cg.set_lineno(node.lineno)
        self.load_name(node.typename)

        # Validate the type of the child
        with cg.try_squash_raise():
            cg.dup_top()
            cg.load_helper_from_fast('validate_declarative')
            cg.rot_two()                            # base -> helper -> base
            cg.call_function(1)                     # base -> retval
            cg.pop_top()                            # base

        # Subclass the child class if needed
        if needs_subclass(node):
            cg.load_const(node.typename)
            cg.rot_two()
            cg.build_tuple(1)
            cg.build_map()
            cg.load_global('__name__')
            cg.load_const('__module__')
            cg.store_map()                          # name -> bases -> dict
            cg.build_class()                        # class

        # Store the class as a local
        cg.dup_top()
        cg.store_fast(class_var)

        # Build the construct node
        cg.load_helper_from_fast('construct_node')
        cg.rot_two()
        cg.load_const(node.identifier)
        cg.load_fast(self.scope_key)
        cg.load_const(self.has_locals())            # helper -> class -> identifier -> key -> bool
        cg.call_function(4)                         # node
        cg.store_fast(node_var)

        # Build an engine for the new class if needed.
        if needs_engine(node):
            cg.load_helper_from_fast('make_engine')
            cg.load_fast(class_var)                 # helper -> class
            cg.call_function(1)                     # engine
            cg.load_fast(class_var)                 # engine -> class
            cg.store_attr('__engine__')

        # Populate the body of the node
        self.class_stack.append(class_var)
        self.node_stack.append(node_var)
        for item in node.body:
            self.visit(item)
        self.class_stack.pop()
        self.node_stack.pop()

        # Add this node to the parent node
        cg.load_fast(self.node_stack[-1])
        cg.load_attr('children')
        cg.load_attr('append')
        cg.load_fast(node_var)
        cg.call_function(1)
        cg.pop_top()

        # Release the held variables
        self.var_pool.release(class_var)
        self.var_pool.release(node_var)

    def visit_StorageExpr(self, node):
        """ The compiler visitor for a StorageExpr node.

        """
        if node.kind == 'static':
            self.visit_StorageExpr_static(node)
            return

        if node.kind == 'const':
            self.visit_StorageExpr_const(node)
            return

        cg = self.code_generator
        cg.set_lineno(node.lineno)
        with self.try_squash_raise():
            cg.load_helper_from_fast('add_storage')
            cg.load_fast(self.class_stack[-1])
            cg.load_const(node.name)
            if node.typename:
                self.load_name(node.typename)
            else:
                cg.load_const(None)
            cg.load_const(node.kind)                # helper -> class -> name -> type -> kind
            cg.call_function(4)                     # retval
            cg.pop_top()

        # Handle the expression binding, if present
        if node.expr is not None:
            self.bind_stack.append(node.name)
            self.visit(node.expr)
            self.bind_stack.pop()

    def visit_StorageExpr_static(self, node):
        """ The compiler visitor for a 'static' StorageExpr node.

        """
        cg = self.code_generator
        cg.set_lineno(node.lineno)

        # Generate the code object for the expression
        expr_cg = CodeGenerator(filename=cg.filename)
        py_node = node.expr.value
        expr_cg.set_lineno(py_node.lineno)
        expr_cg.insert_python_expr(py_node.ast, trim=False)
        call_args = expr_cg.rewrite_to_fast_locals(self.local_names())
        expr_code = expr_cg.to_code(
            args=call_args, newlocals=True, name=node.name,
            firstlineno=py_node.lineno
        )

        with cg.try_squash_raise():

            # Preload the helper and context
            cg.load_helper_from_fast('add_static_attr')
            cg.load_fast(self.class_stack[-1])
            cg.load_const(node.name)                # helper -> class -> name

            # Create and invoke the expression function
            cg.load_const(expr_code)
            cg.make_function()                      # TOS -> func
            for arg in call_args:
                self.load_name(arg)
            cg.call_function(len(call_args))        # TOS -> value

            # Validate the type of the value if necessary
            if node.typename:
                cg.load_helper_from_fast('type_check_expr')
                cg.rot_two()
                self.load_name(node.typename)
                cg.call_function(2)                 # TOS -> value

            # Invoke the helper to add the static attribute
            cg.call_function(3)
            cg.pop_top()

    def visit_StorageExpr_const(self, node):
        """ The compiler visitor for a 'const' StorageExpr node.

        """
        cg = self.code_generator
        cg.set_lineno(node.lineno)

        # Generate the code object for the expression
        expr_cg = CodeGenerator(filename=cg.filename)
        py_node = node.expr.value
        expr_cg.set_lineno(py_node.lineno)
        expr_cg.insert_python_expr(py_node.ast, trim=False)
        call_args = expr_cg.rewrite_to_fast_locals(self.local_names())
        expr_code = expr_cg.to_code(
            args=call_args, newlocals=True, name=node.name,
            firstlineno=py_node.lineno
        )

        with cg.try_squash_raise():

            # Create and invoke the expression function
            cg.load_const(expr_code)
            cg.make_function()                      # TOS -> func
            for arg in call_args:
                self.load_name(arg)
            cg.call_function(len(call_args))        # TOS -> value

            # Validate the type of the value if necessary
            if node.typename:
                cg.load_helper_from_fast('type_check_expr')
                cg.rot_two()
                self.load_name(node.typename)
                cg.call_function(2)                 # TOS -> value

            # Store the value as a fast local
            cg.store_fast(node.name)

    def visit_Binding(self, node):
        """ The compiler visitor for a Binding node.

        """
        self.bind_stack.append(node.name)
        self.visit(node.expr)
        self.bind_stack.pop()

    def visit_OperatorExpr(self, node):
        """ The compiler visitor for an OperatorExpr node.

        """
        cg = self.code_generator
        self.visit(node.value)
        code = self.code_stack.pop()
        cg.set_lineno(node.lineno)
        with cg.try_squash_raise():
            cg.load_helper_from_fast('run_operator')
            cg.load_fast(self.node_stack[-1])
            cg.load_const(self.bind_stack[-1])
            cg.load_const(node.operator)
            cg.load_const(code)
            cg.load_globals_from_fast()             # helper -> node -> name -> op -> code -> globals
            cg.call_function(5)
            cg.pop_top()

    def visit_PythonExpression(self, node):
        """ The compiler visitor for a PythonExpression node.

        """
        cg = self.code_generator
        code = compile(node.ast, cg.filename, mode='eval')
        self.code_stack.append(code)

    def visit_PythonModule(self, node):
        """ The compiler visitor for a PythonModule node.

        """
        cg = self.code_generator
        code = compile(node.ast, cg.filename, mode='exec')
        self.code_stack.append(code)
